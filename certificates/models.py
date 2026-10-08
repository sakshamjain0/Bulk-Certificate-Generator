import uuid

from django.db import models
from django.db.models import Count, Q


class CertificateJobQuerySet(models.QuerySet):
    def with_counts(self):
        """Annotate per-status certificate counts in a single query.

        Avoids an extra aggregate query per job when listing jobs.
        """
        return self.annotate(
            **{
                f"count_{status}": Count("certificates", filter=Q(certificates__status=status))
                for status in Certificate.Status.values
            }
        )


class CertificateJob(models.Model):
    """A single bulk request: shared certificate info + many recipients."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"  # every recipient succeeded
        COMPLETED_WITH_ERRORS = "completed_with_errors", "Completed with errors"  # some failed/invalid
        FAILED = "failed", "Failed"  # nothing was generated

    FINISHED_STATUSES = (Status.COMPLETED, Status.COMPLETED_WITH_ERRORS, Status.FAILED)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Certificate information shared by every recipient in the job.
    course_name = models.CharField(max_length=200)
    issuer_name = models.CharField(max_length=100)
    issue_date = models.DateField()
    description = models.CharField(max_length=200, blank=True)

    status = models.CharField(max_length=32, choices=Status.choices, default=Status.PENDING, db_index=True)
    total_recipients = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    objects = CertificateJobQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Job {self.id} ({self.status})"

    @property
    def is_finished(self) -> bool:
        return self.status in self.FINISHED_STATUSES

    def status_counts(self) -> dict:
        """Return {certificate_status: count}. Uses annotations if present."""
        statuses = Certificate.Status.values
        if all(hasattr(self, f"count_{s}") for s in statuses):
            return {s: getattr(self, f"count_{s}") for s in statuses}

        counts = dict.fromkeys(statuses, 0)
        for row in self.certificates.values("status").annotate(n=Count("id")):
            counts[row["status"]] = row["n"]
        return counts


def certificate_upload_path(instance, filename):
    return f"certificates/{instance.job_id}/{filename}"


class Certificate(models.Model):
    """One recipient within a job, and the outcome of generating their certificate."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"  # valid, waiting to be generated
        SUCCESS = "success", "Success"  # PDF generated and stored
        FAILED = "failed", "Failed"  # valid data, but generation raised an error (retryable)
        INVALID = "invalid", "Invalid"  # rejected at validation time (not retryable)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(CertificateJob, on_delete=models.CASCADE, related_name="certificates")
    # Index of the recipient in the submitted list, so clients can map results back.
    position = models.PositiveIntegerField()

    recipient_name = models.CharField(max_length=120, blank=True)
    # CharField (not EmailField) because invalid rows are stored too.
    recipient_email = models.CharField(max_length=254, blank=True)
    raw_data = models.JSONField(default=dict, blank=True)  # recipient exactly as submitted

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    error_message = models.TextField(blank=True)
    file = models.FileField(upload_to=certificate_upload_path, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    generated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["job", "position"], name="unique_position_per_job"),
        ]
        indexes = [models.Index(fields=["job", "status"])]

    def __str__(self):
        return f"{self.recipient_name or '?'} <{self.recipient_email}> ({self.status})"
