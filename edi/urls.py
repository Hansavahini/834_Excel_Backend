from django.urls import path

from .api.views import (
    ConversionHistoryView,
    Convert834View,
    DownloadView,
    EDIUploadView,
    GeneratedFilePreviewView,
    HealthCheckView,
    MappingCreateView,
    UploadListView,
    UploadSourceView,
    ValidateView,
)

urlpatterns = [
    path("health/", HealthCheckView.as_view(), name="health"),
    path("upload/", EDIUploadView.as_view(), name="upload"),
    path("uploads/", UploadListView.as_view(), name="uploads"),
    path("uploads/<int:pk>/content/", UploadSourceView.as_view(), name="upload-source"),
    path("validate/", ValidateView.as_view(), name="validate"),
    path("mappings/", MappingCreateView.as_view(), name="mappings"),
    path("convert/", Convert834View.as_view(), name="convert"),
    path("download/<int:pk>/", DownloadView.as_view(), name="download"),
    path("download/<int:pk>/preview/", GeneratedFilePreviewView.as_view(), name="download-preview"),
    path("history/", ConversionHistoryView.as_view(), name="history"),
]
