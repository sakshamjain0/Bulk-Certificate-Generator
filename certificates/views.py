import logging
import tempfile
import zipfile

from django.http import FileResponse, HttpResponse
from django.utils import timezone
from django.utils.text import slugify
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.negotiation import BaseContentNegotiation
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Certificate, CertificateJob
from .pdf import CertificateData, CertificateRenderError, render_certificate_pdf
from .serializers import (
    CertificateJobCreateSerializer,
    CertificateJobDetailSerializer,
    CertificateJobSerializer,
    CertificateSerializer,
    TemplatePreviewSerializer,
)
from .tasks import enqueue_job

logger = logging.getLogger(__name__)

TRUE_VALUES = {"1", "true", "yes"}


class FileDownloadNegotiation(BaseContentNegotiation):
    """Content negotiation for endpoints that return files.

    Clients (Swagger UI, browsers, curl -H "Accept: application/pdf") send an
    Accept header of the *file* type. DRF only has JSON renderers, so normal
    negotiation would answer 406 before the view runs. Files are returned as
    plain Django FileResponses (not rendered), and error bodies always use the
    first renderer (JSON).
    """

    def select_parser(self, request, parsers):
        return parsers[0]

    def select_renderer(self, request, renderers, format_suffix=None):
        return renderers[0], renderers[0].media_type


def _download_filename(cert: Certificate) -> str:
    name = slugify(cert.recipient_name) or "certificate"
    return f"{cert.position:05d}_{name}.pdf"


def _is_true(value) -> bool:
    return str(value or "").lower() in TRUE_VALUES


# --- Reusable OpenAPI fragments ----------------------------------------------

ERROR_RESPONSE = OpenApiResponse(OpenApiTypes.OBJECT, description='Error, e.g. `{"detail": "..."}`')

SAMPLE_REQUEST = OpenApiExample(
    "Mixed valid / invalid recipients",
    request_only=True,
    value={
        "course_name": "Advanced Django & REST APIs",
        "issuer_name": "Acme Academy",
        "issue_date": "2026-10-01",
        "description": "Awarded for completing 40 hours of hands-on training.",
        "recipients": [
            {"name": "Alice Smith", "email": "alice@example.com"},
            {"name": "Bob Jones", "email": "bob@example.com"},
            {"name": "Carol White", "email": "not-an-email"},
            {"name": "", "email": "dave@example.com"},
        ],
    },
)


@extend_schema_view(
    list=extend_schema(summary="List jobs", tags=["Jobs"]),
    retrieve=extend_schema(
        summary="Job status & progress",
        description="Overall status, progress counts and (up to 100) failed/invalid recipients with reasons. "
                    "Poll this endpoint until `status` is `completed`, `completed_with_errors` or `failed`.",
        tags=["Jobs"],
    ),
)
class CertificateJobViewSet(mixins.CreateModelMixin,
                            mixins.ListModelMixin,
                            mixins.RetrieveModelMixin,
                            viewsets.GenericViewSet):
    """
    POST /api/jobs/                       create a bulk generation job (202)
    GET  /api/jobs/                       list jobs
    GET  /api/jobs/{id}/                  job status, progress and failures
    GET  /api/jobs/{id}/certificates/     paginated per-recipient results (?status=)
    GET  /api/jobs/{id}/download/         ZIP of all generated certificates
    POST /api/jobs/{id}/retry/            re-run failed certificates
    """

    def get_queryset(self):
        # Explicit order_by: Django ignores Meta.ordering on GROUP BY (annotated)
        # queries, which would make pagination non-deterministic.
        return CertificateJob.objects.with_counts().order_by("-created_at")

    def get_serializer_class(self):
        if self.action == "create":
            return CertificateJobCreateSerializer
        if self.action == "retrieve":
            return CertificateJobDetailSerializer
        if self.action == "certificates":
            return CertificateSerializer
        return CertificateJobSerializer

    def _detail_response(self, job_id, status_code):
        job = self.get_queryset().get(pk=job_id)
        data = CertificateJobDetailSerializer(job, context=self.get_serializer_context()).data
        return Response(data, status=status_code, headers={"Location": data["links"]["self"]})

    @extend_schema(
        summary="Submit a bulk certificate generation request",
        description=(
            "Validates the request and every recipient, stores the job and returns **202 Accepted** "
            "immediately; certificates are generated in the background.\n\n"
            "* Request-level problems (missing fields, empty/oversized list) → `400`, nothing created.\n"
            "* Invalid recipients are recorded with status `invalid` and a reason; valid ones are generated.\n"
            "* All recipients invalid, or any invalid with `?strict=true` → `400`."
        ),
        tags=["Jobs"],
        parameters=[OpenApiParameter("strict", OpenApiTypes.BOOL, description="Reject the whole request if any recipient is invalid.")],
        request=CertificateJobCreateSerializer,
        responses={202: CertificateJobDetailSerializer, 400: ERROR_RESPONSE},
        examples=[SAMPLE_REQUEST],
    )
    def create(self, request, *args, **kwargs):
        serializer = CertificateJobCreateSerializer(
            data=request.data,
            context={**self.get_serializer_context(), "strict": _is_true(request.query_params.get("strict"))},
        )
        serializer.is_valid(raise_exception=True)
        job = serializer.save()
        enqueue_job(job.id)
        # 202 Accepted: the job is queued; poll the Location URL for progress.
        return self._detail_response(job.id, status.HTTP_202_ACCEPTED)

    @extend_schema(
        summary="Per-recipient results of a job",
        tags=["Jobs"],
        parameters=[OpenApiParameter("status", OpenApiTypes.STR, enum=Certificate.Status.values,
                                     description="Only return certificates with this status.")],
        responses={200: CertificateSerializer(many=True), 400: ERROR_RESPONSE},
    )
    @action(detail=True, methods=["get"])
    def certificates(self, request, pk=None):
        job = self.get_object()
        qs = job.certificates.all()

        status_filter = request.query_params.get("status")
        if status_filter:
            if status_filter not in Certificate.Status.values:
                return Response(
                    {"status": [f"Must be one of: {', '.join(Certificate.Status.values)}."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            qs = qs.filter(status=status_filter)

        page = self.paginate_queryset(qs)
        serializer = CertificateSerializer(page, many=True, context=self.get_serializer_context())
        return self.get_paginated_response(serializer.data)

    @extend_schema(
        summary="Download all generated certificates (ZIP)",
        tags=["Jobs"],
        responses={(200, "application/zip"): OpenApiTypes.BINARY, 404: ERROR_RESPONSE},
    )
    @action(detail=True, methods=["get"], content_negotiation_class=FileDownloadNegotiation, pagination_class=None)
    def download(self, request, pk=None):
        """Stream a ZIP with every successfully generated certificate so far."""
        job = self.get_object()
        certs = job.certificates.filter(status=Certificate.Status.SUCCESS).exclude(file="")
        if not certs.exists():
            return Response({"detail": "No generated certificates are available for this job yet."},
                            status=status.HTTP_404_NOT_FOUND)

        # Spool to a temp file rather than memory: large jobs can be big.
        archive = tempfile.TemporaryFile()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for cert in certs.iterator():
                try:
                    with cert.file.open("rb") as fh:
                        zf.writestr(_download_filename(cert), fh.read())
                except FileNotFoundError:
                    logger.warning("File missing for certificate %s", cert.id)
        archive.seek(0)
        return FileResponse(archive, as_attachment=True, filename=f"certificates_{job.id}.zip",
                            content_type="application/zip")

    @extend_schema(
        summary="Retry failed certificates",
        description="Re-queues certificates with status `failed`. `invalid` ones are skipped "
                    "(they need corrected data). Successful certificates are untouched.",
        tags=["Jobs"],
        request=None,
        responses={202: CertificateJobDetailSerializer, 400: ERROR_RESPONSE, 409: ERROR_RESPONSE},
    )
    @action(detail=True, methods=["post"])
    def retry(self, request, pk=None):
        """Re-queue FAILED certificates (INVALID ones need corrected data, so are skipped)."""
        job = self.get_object()
        if not job.is_finished:
            return Response({"detail": "Job is still running."}, status=status.HTTP_409_CONFLICT)

        # Conditional update guards against two concurrent retry calls.
        claimed = CertificateJob.objects.filter(
            pk=job.pk, status__in=CertificateJob.FINISHED_STATUSES
        ).update(status=CertificateJob.Status.PENDING, completed_at=None)
        if not claimed:
            return Response({"detail": "Job is still running."}, status=status.HTTP_409_CONFLICT)

        reset = job.certificates.filter(status=Certificate.Status.FAILED).update(
            status=Certificate.Status.PENDING, error_message=""
        )
        if not reset:
            # Nothing to do: restore the previous status.
            CertificateJob.objects.filter(pk=job.pk).update(status=job.status, completed_at=job.completed_at)
            return Response({"detail": "There are no failed certificates to retry."},
                            status=status.HTTP_400_BAD_REQUEST)

        enqueue_job(job.id)
        return self._detail_response(job.id, status.HTTP_202_ACCEPTED)


@extend_schema_view(retrieve=extend_schema(summary="Certificate status", tags=["Certificates"]))
class CertificateViewSet(mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """
    GET /api/certificates/{id}/           certificate status/details
    GET /api/certificates/{id}/download/  the PDF file (?inline=true to view in the browser)
    """

    queryset = Certificate.objects.all()
    serializer_class = CertificateSerializer

    @extend_schema(
        summary="Download a certificate PDF",
        description="`409` while the certificate is still pending; `404` (with the reason) if it failed or was invalid.",
        tags=["Certificates"],
        parameters=[OpenApiParameter("inline", OpenApiTypes.BOOL,
                                     description="Display in the browser instead of downloading.")],
        responses={(200, "application/pdf"): OpenApiTypes.BINARY, 404: ERROR_RESPONSE, 409: ERROR_RESPONSE},
    )
    @action(detail=True, methods=["get"], content_negotiation_class=FileDownloadNegotiation)
    def download(self, request, pk=None):
        cert = self.get_object()
        if cert.status == Certificate.Status.PENDING:
            return Response({"detail": "Certificate has not been generated yet.", "status": cert.status},
                            status=status.HTTP_409_CONFLICT)
        if cert.status != Certificate.Status.SUCCESS or not cert.file:
            return Response(
                {"detail": "Certificate was not generated.", "status": cert.status, "error": cert.error_message},
                status=status.HTTP_404_NOT_FOUND,
            )
        try:
            fh = cert.file.open("rb")
        except FileNotFoundError:
            return Response({"detail": "Certificate file is missing."}, status=status.HTTP_404_NOT_FOUND)
        return FileResponse(fh, as_attachment=not _is_true(request.query_params.get("inline")),
                            filename=_download_filename(cert), content_type="application/pdf")


class TemplatePreviewView(APIView):
    """Render the certificate template with sample data, without creating a job."""

    content_negotiation_class = FileDownloadNegotiation

    @extend_schema(
        summary="Preview the certificate template",
        description="Renders the predefined template with the given values and returns the PDF inline. "
                    "Nothing is stored. Useful to check the design before submitting a bulk job.",
        tags=["Template"],
        parameters=[TemplatePreviewSerializer],
        responses={(200, "application/pdf"): OpenApiTypes.BINARY, 400: ERROR_RESPONSE},
    )
    def get(self, request):
        params = TemplatePreviewSerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        data = params.validated_data
        try:
            pdf_bytes = render_certificate_pdf(CertificateData(
                certificate_id="PREVIEW",
                recipient_name=data["name"],
                course_name=data["course_name"],
                issuer_name=data["issuer_name"],
                issue_date=data.get("issue_date") or timezone.localdate(),
                description=data["description"],
            ))
        except CertificateRenderError as exc:
            raise serializers.ValidationError({"detail": str(exc)})
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = 'inline; filename="certificate_preview.pdf"'
        return response
