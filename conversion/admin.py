from django.contrib import admin

from .models import ConversionHistory, FileComparison


@admin.register(ConversionHistory)
class ConversionHistoryAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "uploaded_file", "mapping_template", "status", "rows_written", "warning_count", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("uploaded_file__original_filename", "error_message")
    autocomplete_fields = ("owner", "uploaded_file", "mapping_template", "generated_file")
    date_hierarchy = "created_at"
    readonly_fields = ("created_at", "started_at", "finished_at", "warnings")

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(FileComparison)
class FileComparisonAdmin(admin.ModelAdmin):
    list_display = ("id", "baseline_file", "current_file", "added_count", "terminated_count", "changed_count", "dropped_count", "created_at")
    autocomplete_fields = ("owner", "baseline_file", "current_file")
    date_hierarchy = "created_at"

    def has_change_permission(self, request, obj=None):
        return False
