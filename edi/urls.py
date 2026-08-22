from django.urls import path

from .api.views import (
    ConversionHistoryView,
    Convert834View,
    DownloadView,
    EDIUploadView,
    HealthCheckView,
    MappingCreateView,
    ValidateView,
)

urlpatterns = [
    path("health/", HealthCheckView.as_view(), name="health"),
    path("upload/", EDIUploadView.as_view(), name="upload"),
    path("validate/", ValidateView.as_view(), name="validate"),
    path("mappings/", MappingCreateView.as_view(), name="mappings"),
    path("convert/", Convert834View.as_view(), name="convert"),
    path("download/<int:pk>/", DownloadView.as_view(), name="download"),
    path("history/", ConversionHistoryView.as_view(), name="history"),
]
