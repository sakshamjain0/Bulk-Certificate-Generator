from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import Certificate, CertificateJob
from .pdf import unsupported_characters

# Max number of failed/invalid recipients embedded in the job detail response.
# The full list is always available via /jobs/{id}/certificates/?status=failed
FAILURE_PREVIEW_LIMIT = 100


def _check_renderable(value: str) -> str:
    bad = unsupported_characters(value)
    if bad:
        raise serializers.ValidationError(
            f"Contains characters not supported by the certificate template: {bad!r}"
        )
    return value


def _flatten_errors(errors) -> str:
    """{'email': ['Enter a valid email address.']} -> 'email: Enter a valid email address.'"""
    parts = []
    for field, messages in errors.items():
        if isinstance(messages, (list, tuple)):
            messages = " ".join(str(m) for m in messages)
        parts.append(f"{field}: {messages}")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------

class RecipientSerializer(serializers.Serializer):
    """Validates a single recipient row."""

    name = serializers.CharField(max_length=120)
    email = serializers.EmailField(max_length=254)

    def validate_name(self, value):
        return _check_renderable(value)


class CertificateJobCreateSerializer(serializers.Serializer):
    """Validates a bulk request.

    Two levels of validation:
      * Request-level problems (missing course name, recipients not a list,
        too many recipients, ...) reject the whole request with 400.
      * Recipient-level problems (bad email, missing name, duplicate) only
        mark that recipient INVALID; the valid ones are still generated.
        With ``?strict=true`` any invalid recipient rejects the request.
    """

    course_name = serializers.CharField(max_length=200, validators=[_check_renderable])
    issuer_name = serializers.CharField(max_length=100, validators=[_check_renderable])
    issue_date = serializers.DateField(required=False)
    description = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default="", validators=[_check_renderable]
    )
    recipients = serializers.ListField(child=serializers.JSONField(), allow_empty=False)

    def validate_recipients(self, value):
        max_recipients = settings.CERTIFICATE_MAX_RECIPIENTS_PER_JOB
        if len(value) > max_recipients:
            raise serializers.ValidationError(
                f"A single job may contain at most {max_recipients} recipients (got {len(value)})."
            )

        rows = []
        seen_emails = {}  # lowercased email -> first position
        for position, raw in enumerate(value):
            row = {"position": position, "raw": raw, "name": "", "email": "", "errors": None}

            if not isinstance(raw, dict):
                row["errors"] = {"recipient": ["Must be an object with 'name' and 'email'."]}
                rows.append(row)
                continue

            # Keep whatever was sent (truncated) so invalid rows remain identifiable.
            row["name"] = str(raw.get("name") or "")[:120]
            row["email"] = str(raw.get("email") or "")[:254]

            recipient = RecipientSerializer(data=raw)
            if not recipient.is_valid():
                row["errors"] = recipient.errors
            else:
                row["name"] = recipient.validated_data["name"]
                row["email"] = recipient.validated_data["email"]
                key = row["email"].lower()
                if key in seen_emails:
                    row["errors"] = {"email": [f"Duplicate of recipient at position {seen_emails[key]}."]}
                else:
                    seen_emails[key] = position
            rows.append(row)

        invalid = [r for r in rows if r["errors"]]
        if invalid and (self.context.get("strict") or len(invalid) == len(rows)):
            reason = (
                "Strict mode: request rejected because some recipients are invalid."
                if len(invalid) < len(rows) else "No valid recipients."
            )
            raise serializers.ValidationError(
                {"message": reason, "invalid_recipients": {str(r["position"]): r["errors"] for r in invalid}}
            )
        return rows

    def create(self, validated_data):
        rows = validated_data.pop("recipients")
        validated_data["issue_date"] = validated_data.get("issue_date") or timezone.localdate()

        with transaction.atomic():
            job = CertificateJob.objects.create(total_recipients=len(rows), **validated_data)
            Certificate.objects.bulk_create(
                [
                    Certificate(
                        job=job,
                        position=row["position"],
                        recipient_name=row["name"],
                        recipient_email=row["email"],
                        raw_data=row["raw"],
                        status=Certificate.Status.INVALID if row["errors"] else Certificate.Status.PENDING,
                        error_message=_flatten_errors(row["errors"]) if row["errors"] else "",
                    )
                    for row in rows
                ],
                batch_size=500,
            )
        return job


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _absolute(request, url):
    return request.build_absolute_uri(url) if request else url


class CertificateSerializer(serializers.ModelSerializer):
    job_id = serializers.UUIDField(read_only=True)
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = Certificate
        fields = [
            "id", "job_id", "position", "recipient_name", "recipient_email",
            "status", "error_message", "generated_at", "download_url",
        ]

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_download_url(self, obj):
        if obj.status != Certificate.Status.SUCCESS or not obj.file:
            return None
        return _absolute(self.context.get("request"), reverse("certificate-download", kwargs={"pk": obj.pk}))


class JobProgressSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    processed = serializers.IntegerField()
    pending = serializers.IntegerField()
    succeeded = serializers.IntegerField()
    failed = serializers.IntegerField()
    invalid = serializers.IntegerField()
    percent = serializers.FloatField()


class JobLinksSerializer(serializers.Serializer):
    self = serializers.URLField()
    certificates = serializers.URLField()
    download_zip = serializers.URLField()


class CertificateJobSerializer(serializers.ModelSerializer):
    progress = serializers.SerializerMethodField()
    links = serializers.SerializerMethodField()

    class Meta:
        model = CertificateJob
        fields = [
            "id", "status", "course_name", "issuer_name", "issue_date", "description",
            "total_recipients", "progress", "created_at", "started_at", "completed_at", "links",
        ]

    @extend_schema_field(JobProgressSerializer)
    def get_progress(self, obj):
        counts = obj.status_counts()
        S = Certificate.Status
        processed = counts[S.SUCCESS] + counts[S.FAILED] + counts[S.INVALID]
        total = obj.total_recipients
        return {
            "total": total,
            "processed": processed,
            "pending": counts[S.PENDING],
            "succeeded": counts[S.SUCCESS],
            "failed": counts[S.FAILED],
            "invalid": counts[S.INVALID],
            "percent": round(processed * 100 / total, 1) if total else 100.0,
        }

    @extend_schema_field(JobLinksSerializer)
    def get_links(self, obj):
        request = self.context.get("request")
        return {
            "self": _absolute(request, reverse("job-detail", kwargs={"pk": obj.pk})),
            "certificates": _absolute(request, reverse("job-certificates", kwargs={"pk": obj.pk})),
            "download_zip": _absolute(request, reverse("job-download", kwargs={"pk": obj.pk})),
        }


class CertificateJobDetailSerializer(CertificateJobSerializer):
    """Job status plus the recipients that did not succeed, with reasons."""

    failures = serializers.SerializerMethodField()

    class Meta(CertificateJobSerializer.Meta):
        fields = CertificateJobSerializer.Meta.fields + ["failures"]

    @extend_schema_field(CertificateSerializer(many=True))
    def get_failures(self, obj):
        qs = obj.certificates.filter(
            status__in=[Certificate.Status.FAILED, Certificate.Status.INVALID]
        ).order_by("position")[:FAILURE_PREVIEW_LIMIT]
        return CertificateSerializer(qs, many=True, context=self.context).data


class TemplatePreviewSerializer(serializers.Serializer):
    """Query parameters for rendering a quick template preview."""

    name = serializers.CharField(max_length=120, default="Jane Doe", required=False, validators=[_check_renderable])
    course_name = serializers.CharField(
        max_length=200, default="Sample Certificate of Achievement", required=False, validators=[_check_renderable]
    )
    issuer_name = serializers.CharField(
        max_length=100, default="Acme Global Academy", required=False, validators=[_check_renderable]
    )
    issue_date = serializers.DateField(required=False)
    description = serializers.CharField(
        max_length=200, default="In recognition of outstanding performance and dedication.", required=False, allow_blank=True, validators=[_check_renderable]
    )

