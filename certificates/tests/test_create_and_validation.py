from django.test import override_settings

from certificates.models import Certificate, CertificateJob

from .base import CertificateAPITestCase, make_payload


class CreateJobTests(CertificateAPITestCase):
    def test_create_job_returns_202_with_job_details(self):
        response = self.create_job()

        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertIn("id", body)
        self.assertEqual(body["total_recipients"], 2)
        self.assertEqual(body["course_name"], "Advanced Django")
        self.assertTrue(response["Location"].endswith(f"/api/jobs/{body['id']}/"))

    def test_create_job_persists_one_certificate_per_recipient_in_order(self):
        response = self.create_job()

        job = CertificateJob.objects.get(pk=response.json()["id"])
        certs = list(job.certificates.order_by("position"))
        self.assertEqual([c.recipient_name for c in certs], ["Alice Smith", "Bob Jones"])
        self.assertEqual([c.position for c in certs], [0, 1])

    def test_issue_date_defaults_to_today(self):
        payload = make_payload()
        del payload["issue_date"]

        response = self.create_job(payload)

        self.assertEqual(response.status_code, 202)
        self.assertIsNotNone(response.json()["issue_date"])


class RequestValidationTests(CertificateAPITestCase):
    def assert_rejected(self, payload, field, query=""):
        response = self.create_job(payload, query)
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn(field, response.json())
        self.assertEqual(CertificateJob.objects.count(), 0)
        return response.json()

    def test_missing_course_name(self):
        payload = make_payload()
        del payload["course_name"]
        self.assert_rejected(payload, "course_name")

    def test_blank_issuer_name(self):
        self.assert_rejected(make_payload(issuer_name="   "), "issuer_name")

    def test_invalid_issue_date(self):
        self.assert_rejected(make_payload(issue_date="31-31-2026"), "issue_date")

    def test_empty_recipient_list(self):
        self.assert_rejected(make_payload(recipients=[]), "recipients")

    def test_recipients_not_a_list(self):
        self.assert_rejected(make_payload(recipients="alice@example.com"), "recipients")

    @override_settings(CERTIFICATE_MAX_RECIPIENTS_PER_JOB=3)
    def test_too_many_recipients(self):
        recipients = [{"name": f"P{i}", "email": f"p{i}@example.com"} for i in range(4)]
        body = self.assert_rejected(make_payload(recipients=recipients), "recipients")
        self.assertIn("at most 3", str(body))

    def test_all_recipients_invalid_is_rejected(self):
        body = self.assert_rejected(
            make_payload(recipients=[{"name": "", "email": "nope"}, {"email": "x@example.com"}]), "recipients"
        )
        self.assertEqual(set(body["recipients"]["invalid_recipients"]), {"0", "1"})

    def test_strict_mode_rejects_request_with_any_invalid_recipient(self):
        recipients = [{"name": "Alice", "email": "alice@example.com"}, {"name": "Bob", "email": "bad"}]
        body = self.assert_rejected(make_payload(recipients=recipients), "recipients", query="?strict=true")
        self.assertIn("1", body["recipients"]["invalid_recipients"])
        self.assertIn("email", body["recipients"]["invalid_recipients"]["1"])

    def test_unsupported_characters_in_course_name(self):
        self.assert_rejected(make_payload(course_name="Kurs 日本語"), "course_name")


class RecipientValidationTests(CertificateAPITestCase):
    """Invalid recipients are recorded, valid ones are still generated."""

    def test_invalid_recipients_are_marked_and_valid_ones_generated(self):
        recipients = [
            {"name": "Alice", "email": "alice@example.com"},   # 0 valid
            {"name": "Bob", "email": "not-an-email"},          # 1 bad email
            {"email": "carol@example.com"},                    # 2 missing name
            {"name": "Alice Again", "email": "ALICE@example.com"},  # 3 duplicate (case-insensitive)
            "just a string",                                   # 4 not an object
            {"name": "王伟", "email": "wang@example.com"},       # 5 unsupported characters
            {"name": "  Dave  ", "email": "dave@example.com"},  # 6 valid (trimmed)
        ]
        response = self.create_job(make_payload(recipients=recipients))

        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body["status"], CertificateJob.Status.COMPLETED_WITH_ERRORS)
        self.assertEqual(body["progress"]["succeeded"], 2)
        self.assertEqual(body["progress"]["invalid"], 5)

        failures = {f["position"]: f for f in body["failures"]}
        self.assertEqual(set(failures), {1, 2, 3, 4, 5})
        self.assertIn("email", failures[1]["error_message"])
        self.assertIn("name", failures[2]["error_message"])
        self.assertIn("Duplicate", failures[3]["error_message"])
        self.assertIn("object", failures[4]["error_message"])
        self.assertIn("not supported", failures[5]["error_message"])
        self.assertEqual(failures[1]["recipient_email"], "not-an-email")  # original kept for identification

        dave = Certificate.objects.get(job_id=body["id"], position=6)
        self.assertEqual(dave.recipient_name, "Dave")
        self.assertEqual(dave.status, Certificate.Status.SUCCESS)

    def test_name_too_long_is_invalid(self):
        recipients = [{"name": "A", "email": "a@example.com"}, {"name": "x" * 121, "email": "b@example.com"}]
        body = self.create_job(make_payload(recipients=recipients)).json()
        self.assertEqual(body["failures"][0]["position"], 1)
        self.assertEqual(body["failures"][0]["status"], Certificate.Status.INVALID)
