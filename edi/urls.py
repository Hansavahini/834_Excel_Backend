from django.urls import path

from .api.views import (
    ConversionHistoryView,
    Convert834View,
    DashboardSummaryView,
    DownloadView,
    EDIFileDownloadView,
    EDIFilePreviewView,
    EDIUploadView,
    GeneratedFilePreviewView,
    HealthCheckView,
    JobDetailView,
    JobStatusView,
    MappingCreateView,
    SegmentDictionaryView,
    UploadListView,
    UploadSourceView,
    ValidateView,
)

urlpatterns = [
    path("health/", HealthCheckView.as_view(), name="health"),
    path("upload/", EDIUploadView.as_view(), name="upload"),
    path("uploads/", UploadListView.as_view(), name="uploads"),
    # Retained: the original combined source endpoint. Still used by older
    # clients and by the contract tests, and still honest about truncation.
    path("uploads/<int:pk>/content/", UploadSourceView.as_view(), name="upload-source"),
    # Part 8: preview and download as separate contracts.
    path("files/<int:pk>/preview/", EDIFilePreviewView.as_view(), name="edi-file-preview"),
    path("files/<int:pk>/download/", EDIFileDownloadView.as_view(), name="edi-file-download"),
    path("validate/", ValidateView.as_view(), name="validate"),
    # The background work the browser polls. Validation and conversion are
    # enqueued, not performed, by their endpoints.
    path("jobs/", JobStatusView.as_view(), name="jobs"),
    path("jobs/<int:pk>/", JobDetailView.as_view(), name="job-detail"),
    path("mappings/", MappingCreateView.as_view(), name="mappings"),
    # Part 6.4: the segment and element dictionary, from the database.
    path("segments/", SegmentDictionaryView.as_view(), name="segments"),
    path("convert/", Convert834View.as_view(), name="convert"),
    path("download/<int:pk>/", DownloadView.as_view(), name="download"),
    path("download/<int:pk>/preview/", GeneratedFilePreviewView.as_view(), name="download-preview"),
    path("history/", ConversionHistoryView.as_view(), name="history"),
    # Part 5: the Information panel's numbers.
    path("dashboard/", DashboardSummaryView.as_view(), name="dashboard-summary"),
]
