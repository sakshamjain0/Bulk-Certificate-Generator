import io
import zipfile
from datetime import date
from unittest import mock

from django.test import SimpleTestCase, override_settings

from certificates import pdf, tasks
from certificates.models import Certificate, CertificateJob
from certificates.pdf import CertificateData, CertificateRenderError, render_certificate_pdf
from certificates.services import process_job
from certificates.tasks import enqueue_job, run_job_safely

from .base import CertificateAPITestCase, make_payload

real_render = render_certificate_pdf


def render_failing_for(*names):
    """A render function that raises for the given recipient names only."""
    def _render(data):
        if data.recipient_name in names:
            raise RuntimeError(f"simulated renderer crash for {data.recipient_name}")
        return real_render(data)
    return _render


# ---------------------------------------------------------------------------
# Certificate generation (template level)
# ---------------------------------------------------------------------------

class PdfTemplateTests(SimpleTestCase):
    def data(self, **overrides):
        values = dict(certificate_id="abc-123", recipient_name="Alice Smith", course_name="Advanced Django",
                      issuer_name="Acme Academy", issue_date=date(2026, 10, 1), description="With distinction")
        values.update(overrides)
        return CertificateData(**values)

    def test_renders_a_pdf_containing_recipient_specific_data(self):
        with mock.patch.object(pdf, "PAGE_COMPRESSION", 0):  # readable content streams
            content = render_certificate_pdf(self.data())

        self.assertTrue(content.startswith(b"%PDF"))
        for text in (b"Alice Smith", b"Advanced Django", b"Acme Academy", b"01 October 2026", b"abc-123"):
            self.assertIn(text, content)

    def test_long_values_are_shrunk_or_wrapped_not_rejected(self):
        content = render_certificate_pdf(self.data(recipient_name="N" * 120, course_name="C" * 200))
        self.assertTrue(content.startswith(b"%PDF"))

    def test_unsupported_characters_raise_clear_error(self):
        with self.assertRaisesMessage(CertificateRenderError, "Recipient name"):
            render_certificate_pdf(self.data(recipient_name="王伟"))


# ---------------------------------------------------------------------------
# Generation through the API + job status / progress
# ---------------------------------------------------------------------------

class GenerationAndStatusTests(CertificateAPITestCase):
    def test_all_certificates_generated_and_job_completed(self):
        job_id = self.create_job().json()["id"]

        response = self.client.get(f"/api/jobs/{job_id}/")
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["status"], CertificateJob.Status.COMPLETED)
        self.assertEqual(body["progress"], {"total": 2, "processed": 2, "pending": 0, "succeeded": 2,
                                            "failed": 0, "invalid": 0, "percent": 100.0})
        self.assertEqual(body["failures"], [])
        self.assertIsNotNone(body["started_at"])
        self.assertIsNotNone(body["completed_at"])

        for cert in Certificate.objects.filter(job_id=job_id):
            self.assertEqual(cert.status, Certificate.Status.SUCCESS)
            self.assertTrue(cert.file.name.endswith(f"{cert.id}.pdf"))
            self.assertIsNotNone(cert.generated_at)

    def test_progress_while_job_is_pending_then_after_processing(self):
        with mock.patch("certificates.views.enqueue_job"):  # accept but don't process yet
            job_id = self.create_job().json()["id"]

        body = self.client.get(f"/api/jobs/{job_id}/").json()
        self.assertEqual(body["status"], CertificateJob.Status.PENDING)
        self.assertEqual(body["progress"]["processed"], 0)
        self.assertEqual(body["progress"]["pending"], 2)
        self.assertEqual(body["progress"]["percent"], 0.0)

        process_job(job_id)

        body = self.client.get(f"/api/jobs/{job_id}/").json()
        self.assertEqual(body["status"], CertificateJob.Status.COMPLETED)
        self.assertEqual(body["progress"]["percent"], 100.0)

    def test_partial_progress_is_reported(self):
        with mock.patch("certificates.views.enqueue_job"):
            job_id = self.create_job().json()["id"]
        Certificate.objects.filter(job_id=job_id, position=0).update(status=Certificate.Status.SUCCESS)
        CertificateJob.objects.filter(pk=job_id).update(status=CertificateJob.Status.PROCESSING)

        body = self.client.get(f"/api/jobs/{job_id}/").json()
        self.assertEqual(body["status"], CertificateJob.Status.PROCESSING)
        self.assertEqual(body["progress"]["processed"], 1)
        self.assertEqual(body["progress"]["percent"], 50.0)

    def test_list_jobs(self):
        self.create_job()
        self.create_job()
        body = self.client.get("/api/jobs/").json()
        self.assertEqual(body["count"], 2)
        self.assertIn("progress", body["results"][0])

    def test_unknown_job_returns_404(self):
        response = self.client.get("/api/jobs/00000000-0000-0000-0000-000000000000/")
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Individual certificate failure
# ---------------------------------------------------------------------------

class IndividualFailureTests(CertificateAPITestCase):
    def payload(self):
        return make_payload(recipients=[
            {"name": "Alice", "email": "alice@example.com"},
            {"name": "Bob", "email": "bob@example.com"},
            {"name": "Carol", "email": "carol@example.com"},
        ])

    @mock.patch("certificates.services.render_certificate_pdf", side_effect=render_failing_for("Bob"))
    def test_one_failure_does_not_stop_other_certificates(self, _render):
        body = self.create_job(self.payload()).json()

        self.assertEqual(body["status"], CertificateJob.Status.COMPLETED_WITH_ERRORS)
        self.assertEqual(body["progress"]["succeeded"], 2)
        self.assertEqual(body["progress"]["failed"], 1)

        [failure] = body["failures"]
        self.assertEqual(failure["recipient_name"], "Bob")
        self.assertEqual(failure["position"], 1)
        self.assertEqual(failure["status"], Certificate.Status.FAILED)
        self.assertIn("simulated renderer crash", failure["error_message"])
        self.assertIsNone(failure["download_url"])

        statuses = dict(Certificate.objects.filter(job_id=body["id"]).values_list("recipient_name", "status"))
        self.assertEqual(statuses, {"Alice": "success", "Bob": "failed", "Carol": "success"})

    @mock.patch("certificates.services.render_certificate_pdf", side_effect=RuntimeError("boom"))
    def test_job_failed_when_every_certificate_fails(self, _render):
        body = self.create_job(self.payload()).json()
        self.assertEqual(body["status"], CertificateJob.Status.FAILED)
        self.assertEqual(body["progress"]["failed"], 3)

    def test_storage_failure_is_isolated_too(self):
        original_save = Certificate.file.field.storage.save

        def flaky_save(name, content, *args, **kwargs):
            flaky_save.calls += 1
            if flaky_save.calls == 2:  # the second recipient's file write fails
                raise OSError("disk full")
            return original_save(name, content, *args, **kwargs)
        flaky_save.calls = 0

        with mock.patch.object(Certificate.file.field.storage, "save", side_effect=flaky_save):
            body = self.create_job(self.payload()).json()

        self.assertEqual(body["progress"]["succeeded"], 2)
        self.assertEqual(body["progress"]["failed"], 1)
        self.assertIn("disk full", body["failures"][0]["error_message"])

    def test_retry_regenerates_only_failed_certificates(self):
        with mock.patch("certificates.services.render_certificate_pdf", side_effect=render_failing_for("Bob")):
            job_id = self.create_job(self.payload()).json()["id"]
        alice_file = Certificate.objects.get(job_id=job_id, recipient_name="Alice").file.name

        response = self.client.post(f"/api/jobs/{job_id}/retry/")

        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body["status"], CertificateJob.Status.COMPLETED)
        self.assertEqual(body["progress"]["succeeded"], 3)
        self.assertEqual(Certificate.objects.get(job_id=job_id, recipient_name="Alice").file.name, alice_file)

    def test_retry_without_failures_is_rejected(self):
        job_id = self.create_job(self.payload()).json()["id"]
        response = self.client.post(f"/api/jobs/{job_id}/retry/")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(CertificateJob.objects.get(pk=job_id).status, CertificateJob.Status.COMPLETED)

    def test_retry_while_running_is_rejected(self):
        with mock.patch("certificates.views.enqueue_job"):
            job_id = self.create_job(self.payload()).json()["id"]
        response = self.client.post(f"/api/jobs/{job_id}/retry/")
        self.assertEqual(response.status_code, 409)

    def test_unexpected_job_crash_marks_job_failed(self):
        with mock.patch("certificates.views.enqueue_job"):
            job_id = self.create_job(self.payload()).json()["id"]

        with mock.patch("certificates.tasks.process_job", side_effect=RuntimeError("db exploded")):
            run_job_safely(job_id)

        job = CertificateJob.objects.get(pk=job_id)
        self.assertEqual(job.status, CertificateJob.Status.FAILED)
        self.assertFalse(job.certificates.filter(status=Certificate.Status.PENDING).exists())
        self.assertIn("db exploded", job.certificates.first().error_message)


# ---------------------------------------------------------------------------
# Retrieving generated certificates
# ---------------------------------------------------------------------------

class RetrievalTests(CertificateAPITestCase):
    def test_download_single_certificate_pdf(self):
        job = self.create_job().json()
        cert = Certificate.objects.get(job_id=job["id"], position=0)

        detail = self.client.get(f"/api/certificates/{cert.id}/").json()
        self.assertEqual(detail["status"], "success")
        self.assertTrue(detail["download_url"].endswith(f"/api/certificates/{cert.id}/download/"))

        response = self.client.get(detail["download_url"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("00000_alice-smith.pdf", response["Content-Disposition"])
        self.assertTrue(self.read_file_response(response).startswith(b"%PDF"))

    def test_list_job_certificates_with_status_filter(self):
        recipients = [{"name": "Alice", "email": "a@example.com"}, {"name": "Bob", "email": "bad"}]
        job_id = self.create_job(make_payload(recipients=recipients)).json()["id"]

        all_certs = self.client.get(f"/api/jobs/{job_id}/certificates/").json()
        self.assertEqual(all_certs["count"], 2)

        ok = self.client.get(f"/api/jobs/{job_id}/certificates/?status=success").json()
        self.assertEqual([c["recipient_name"] for c in ok["results"]], ["Alice"])
        self.assertIsNotNone(ok["results"][0]["download_url"])

        bad = self.client.get(f"/api/jobs/{job_id}/certificates/?status=invalid").json()
        self.assertEqual([c["recipient_name"] for c in bad["results"]], ["Bob"])

    def test_invalid_status_filter(self):
        job_id = self.create_job().json()["id"]
        response = self.client.get(f"/api/jobs/{job_id}/certificates/?status=bogus")
        self.assertEqual(response.status_code, 400)

    def test_certificates_are_paginated(self):
        recipients = [{"name": f"Person {i}", "email": f"p{i}@example.com"} for i in range(5)]
        job_id = self.create_job(make_payload(recipients=recipients)).json()["id"]
        page = self.client.get(f"/api/jobs/{job_id}/certificates/?page_size=2&page=2").json()
        self.assertEqual(page["count"], 5)
        self.assertEqual([c["position"] for c in page["results"]], [2, 3])

    def test_download_job_zip_contains_only_successful_certificates(self):
        recipients = [
            {"name": "Alice", "email": "a@example.com"},
            {"name": "Bob", "email": "b@example.com"},
            {"name": "Carol", "email": "c@example.com"},
            {"name": "Dan", "email": "invalid"},
        ]
        with mock.patch("certificates.services.render_certificate_pdf", side_effect=render_failing_for("Bob")):
            job_id = self.create_job(make_payload(recipients=recipients)).json()["id"]

        response = self.client.get(f"/api/jobs/{job_id}/download/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        archive = zipfile.ZipFile(io.BytesIO(self.read_file_response(response)))
        self.assertEqual(sorted(archive.namelist()), ["00000_alice.pdf", "00002_carol.pdf"])
        self.assertTrue(archive.read("00000_alice.pdf").startswith(b"%PDF"))

    def test_zip_404_when_nothing_generated_yet(self):
        with mock.patch("certificates.views.enqueue_job"):
            job_id = self.create_job().json()["id"]
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}/download/").status_code, 404)

    def test_download_pending_certificate_returns_409(self):
        with mock.patch("certificates.views.enqueue_job"):
            job_id = self.create_job().json()["id"]
        cert = Certificate.objects.filter(job_id=job_id).first()
        self.assertEqual(self.client.get(f"/api/certificates/{cert.id}/download/").status_code, 409)

    @mock.patch("certificates.services.render_certificate_pdf", side_effect=render_failing_for("Bob Jones"))
    def test_download_failed_certificate_returns_404_with_reason(self, _render):
        job_id = self.create_job().json()["id"]
        cert = Certificate.objects.get(job_id=job_id, recipient_name="Bob Jones")
        response = self.client.get(f"/api/certificates/{cert.id}/download/")
        self.assertEqual(response.status_code, 404)
        self.assertIn("simulated renderer crash", response.json()["error"])


# ---------------------------------------------------------------------------
# Background scheduling
# ---------------------------------------------------------------------------

class SchedulingTests(CertificateAPITestCase):
    @override_settings(CERTIFICATE_PROCESSING_MODE="thread")
    def test_thread_mode_returns_immediately_and_submits_after_commit(self):
        executor = mock.Mock()
        with mock.patch.object(tasks, "_get_executor", return_value=executor):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.create_job()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], CertificateJob.Status.PENDING)  # not processed inline
        executor.submit.assert_called_once()
        func, job_id = executor.submit.call_args.args
        self.assertIs(func, tasks._run_job_in_thread)
        self.assertEqual(str(job_id), response.json()["id"])

    @override_settings(CERTIFICATE_PROCESSING_MODE="nonsense")
    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            enqueue_job("x")


# ---------------------------------------------------------------------------
# OpenAPI / Swagger and Template Preview
# ---------------------------------------------------------------------------

class SwaggerAndPreviewTests(CertificateAPITestCase):
    def test_openapi_schema_endpoint(self):
        response = self.client.get("/api/schema/?format=json")
        self.assertEqual(response.status_code, 200)
        self.assertIn("openapi", response.json())
        self.assertEqual(response.json()["info"]["title"], "Bulk Certificate Generator API")

    def test_swagger_ui_endpoint(self):
        response = self.client.get("/api/docs/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"swagger-ui", response.content.lower())

    def test_redoc_endpoint(self):
        response = self.client.get("/api/redoc/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"redoc", response.content.lower())

    def test_root_redirects_to_swagger_ui(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].endswith("/api/docs/"))

    def test_template_preview_generates_pdf_inline(self):
        response = self.client.get(
            "/api/template/preview/",
            {"name": "Grace Hopper", "course_name": "Compilers & Architecture"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("inline", response["Content-Disposition"])
        content = self.read_file_response(response)
        self.assertTrue(content.startswith(b"%PDF"))

    def test_template_preview_rejects_unsupported_characters(self):
        response = self.client.get("/api/template/preview/", {"name": "王伟"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("name", response.json())

    def test_certificate_download_supports_inline_view(self):
        job = self.create_job().json()
        cert = Certificate.objects.get(job_id=job["id"], position=0)
        response = self.client.get(f"/api/certificates/{cert.id}/download/?inline=true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("inline", response["Content-Disposition"])

