from django.urls import path

from .views import (
    ClientListView,
    CSRFView,
    LoginView,
    LogoutView,
    PasswordChangeView,
    SessionView,
)

urlpatterns = [
    path("csrf/", CSRFView.as_view(), name="api-csrf"),
    path("login/", LoginView.as_view(), name="api-login"),
    path("logout/", LogoutView.as_view(), name="api-logout"),
    path("me/", SessionView.as_view(), name="api-session"),
    path("clients/", ClientListView.as_view(), name="api-clients"),
    path("change-password/", PasswordChangeView.as_view(), name="api-change-password"),
]
