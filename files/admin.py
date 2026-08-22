from django.contrib import admin

from .models import GeneratedFile, UploadedFile


@admin.register(UploadedFile)
class UploadedFileAdmin(admin.ModelAdmin):
    list_display = ("original_filename", "owner", "file_date", "processing_status", "member_loop_count", "uploaded_at")
    list_filter = ("processing_status", "is_full_file", "file_date")
    search_fields = ("original_filename", "content_sha256", "interchange_control_number", "sponsor_name")
    date_hierarchy = "uploaded_at"
    autocomplete_fields = ("owner",)
    readonly_fields = (
        "content_sha256", "file_size_bytes", "uploaded_at", "processing_started_at",
        "processing_finished_at", "segment_count", "member_loop_count",
    )

    def has_delete_permission(self, request, obj=None):
        # Eligibility rows reference uploads with PROTECT; deleting an upload
        # would orphan the audit trail even where the database allows it.
        return False


@admin.register(GeneratedFile)
class GeneratedFileAdmin(admin.ModelAdmin):
    list_display = ("generated_filename", "owner", "uploaded_file", "row_count", "generated_at", "downloaded_count")
    list_filter = ("file_format", "generated_at")
    search_fields = ("generated_filename",)
    autocomplete_fields = ("owner", "uploaded_file")
    readonly_fields = ("generated_at", "downloaded_count", "last_downloaded_at", "file_size_bytes")
