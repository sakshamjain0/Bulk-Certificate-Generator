from django.contrib import admin

from .models import Certificate, CertificateJob


class CertificateInline(admin.TabularInline):
    model = Certificate
    extra = 0
    fields = ("position", "recipient_name", "recipient_email", "status", "error_message", "file")
    readonly_fields = fields
    can_delete = False


@admin.register(CertificateJob)
class CertificateJobAdmin(admin.ModelAdmin):
    list_display = ("id", "course_name", "status", "total_recipients", "created_at", "completed_at")
    list_filter = ("status",)
    inlines = [CertificateInline]


@admin.register(Certificate)
class CertificateAdmin(admin.ModelAdmin):
    list_display = ("id", "job", "position", "recipient_name", "recipient_email", "status")
    list_filter = ("status",)
    search_fields = ("recipient_name", "recipient_email")
