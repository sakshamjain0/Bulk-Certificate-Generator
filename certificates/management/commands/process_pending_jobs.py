from django.core.management.base import BaseCommand

from certificates.models import CertificateJob
from certificates.tasks import run_job_safely


class Command(BaseCommand):
    help = (
        "Process jobs left PENDING/PROCESSING (e.g. after a server restart, since the "
        "in-process thread pool does not survive restarts). Run while the API server "
        "is stopped to avoid processing the same job twice."
    )

    def add_arguments(self, parser):
        parser.add_argument("--job", help="Only process this job id.")

    def handle(self, *args, **options):
        jobs = CertificateJob.objects.filter(
            status__in=[CertificateJob.Status.PENDING, CertificateJob.Status.PROCESSING]
        )
        if options["job"]:
            jobs = jobs.filter(pk=options["job"])

        job_ids = list(jobs.order_by("created_at").values_list("id", flat=True))
        if not job_ids:
            self.stdout.write("No unfinished jobs.")
            return

        for job_id in job_ids:
            self.stdout.write(f"Processing job {job_id} ...")
            run_job_safely(job_id)
            job = CertificateJob.objects.get(pk=job_id)
            self.stdout.write(self.style.SUCCESS(f"  -> {job.status}"))
