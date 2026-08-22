from django.urls import path

from .api.views import (
    HealthCheckView,
    EDIUploadView
)
from .api.views import MappingCreateView
from .api.views import Convert834View
urlpatterns = [

    path(
        "health/",
        HealthCheckView.as_view(),
        name="health"
    ),

    path(
        "upload/",
        EDIUploadView.as_view(),
        name="upload"
    ),

    path(
        "mappings/",
        MappingCreateView.as_view(),
        name="mappings"
    ),
    path(
    "convert/",
    Convert834View.as_view(),
    name="convert"
    ),

]