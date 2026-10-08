"""Core job-processing logic. Independent of how it is scheduled (see tasks.py)."""
import logging

from django.core.files.base import ContentFile
from django.utils import timezone

from .models import Certificate, CertificateJob
from .pdf import CertificateData, render_certificate_pdf

logger = logging.getLogger(__name__)

BATCH_SIZE = 100


def generate_certificate(cert: Certificate) -> bool:
    """Generate and store the PDF for one certificate.

    Never raises for generation errors: the failure is recorded on the
    certificate so the rest of the job can continue.
    """
    job = cert.job
    try:
        pdf_bytes = render_certificate_pdf(
            CertificateData(
                certificate_id=str(cert.id),
                recipient_name=cert.recipient_name,
                course_name=job.course_name,
                issuer_name=job.issuer_name,
                issue_date=job.issue_date,
                description=job.description,
            )
        )
        if cert.file:  # e.g. a retry: replace any previous file
            cert.file.delete(save=False)
        cert.file.save(f"{cert.id}.pdf", ContentFile(pdf_bytes), save=False)
    except Exception as exc:  # noqa: BLE001 - isolate any per-certificate failure
        logger.exception("Certificate %s (job %s) failed", cert.id, cert.job_id)
        cert.status = Certificate.Status.FAILED
        cert.error_message = f"{type(exc).__name__}: {exc}"[:1000]
        cert.save(update_fields=["status", "error_message"])
        return False

    cert.status = Certificate.Status.SUCCESS
    cert.error_message = ""
    cert.generated_at = timezone.now()
    cert.save(update_fields=["status", "error_message", "file", "generated_at"])
    return True


def process_job(job_id) -> CertificateJob:
    """Generate every pending certificate of a job, then finalise its status.

    Each certificate is committed individually, so progress is visible to
    clients polling the job while it runs, and a crash mid-job loses at most
    the certificate being worked on. Re-running is safe: only PENDING
    certificates are processed.
    """
    job = CertificateJob.objects.get(pk=job_id)
    job.status = CertificateJob.Status.PROCESSING
    job.started_at = job.started_at or timezone.now()
    job.save(update_fields=["status", "started_at"])
    logger.info("Processing job %s", job.id)

    pending_ids = list(
        job.certificates.filter(status=Certificate.Status.PENDING).order_by("position").values_list("id", flat=True)
    )
    # Load in batches to bound memory for very large jobs.
    for start in range(0, len(pending_ids), BATCH_SIZE):
        batch_ids = pending_ids[start:start + BATCH_SIZE]
        for cert in Certificate.objects.filter(id__in=batch_ids).select_related("job").order_by("position"):
            generate_certificate(cert)

    return finalize_job(job)


def finalize_job(job: CertificateJob) -> CertificateJob:
    counts = job.status_counts()
    problems = counts[Certificate.Status.FAILED] + counts[Certificate.Status.INVALID]

    if counts[Certificate.Status.SUCCESS] == 0:
        job.status = CertificateJob.Status.FAILED
    elif problems:
        job.status = CertificateJob.Status.COMPLETED_WITH_ERRORS
    else:
        job.status = CertificateJob.Status.COMPLETED
    job.completed_at = timezone.now()
    job.save(update_fields=["status", "completed_at"])
    logger.info("Job %s finished: %s %s", job.id, job.status, counts)
    return job


def abort_job(job_id, reason: str) -> None:
    """Mark a job as failed after an unexpected (non per-certificate) error."""
    Certificate.objects.filter(job_id=job_id, status=Certificate.Status.PENDING).update(
        status=Certificate.Status.FAILED, error_message=f"Job aborted: {reason}"[:1000]
    )
    CertificateJob.objects.filter(pk=job_id).update(
        status=CertificateJob.Status.FAILED, completed_at=timezone.now()
    )
