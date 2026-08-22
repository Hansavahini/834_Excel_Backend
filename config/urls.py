from django.contrib import admin
from django.urls import path, include
from django.http import JsonResponse


def home(request):
    return JsonResponse({
        "service": "834 EDI Converter",
        "status": "running"
    })


urlpatterns = [

    path("", home),

    path(
        "admin/",
        admin.site.urls
    ),

    path(
        "api/edi/",
        include("edi.urls")
    ),
]