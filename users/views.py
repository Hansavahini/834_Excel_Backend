"""
Session endpoints for the SPA.

The original module offered login and nothing else, which left the front end
with no way to log out, no way to discover an existing session on a page
refresh, and no way to obtain a CSRF token before its first unsafe request. All
three are added here. Roles are read from the Django user rather than chosen in
the browser, so an operator cannot promote themselves to admin by picking a tab.
"""

from django.contrib.auth import authenticate, login, logout
from django.middleware.csrf import get_token
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Client, ClientMembership
from .tenancy import default_client, resolve_client, user_clients


def _client_payload(client):
    return {"id": client.id, "code": client.code, "name": client.name}


def describe(user):
    """
    The identity payload the SPA stores. `is_admin` gates the Info section.

    The clients list is part of identity rather than a separate lookup, because
    the browser needs to know which plans it may act for before it renders the
    selector — and because sending the list from the server is what stops the
    selector being a free-text field the backend later trusts.
    """
    is_admin = bool(user.is_staff or user.is_superuser)
    full_name = user.get_full_name() or user.username
    clients = [_client_payload(client) for client in user_clients(user)]
    active = default_client(user)
    return {
        "username": user.username,
        "display_name": full_name,
        "email": user.email,
        "is_admin": is_admin,
        "role": "Platform Admin" if is_admin else "Client User",
        "user_id": user.id,
        "clients": clients,
        "default_client": _client_payload(active) if active else None,
    }


@method_decorator(ensure_csrf_cookie, name="dispatch")
class CSRFView(APIView):
    """Hand the SPA a csrftoken cookie before it attempts its first POST."""

    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"csrfToken": get_token(request)})


@method_decorator(ensure_csrf_cookie, name="dispatch")
class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        username = (request.data.get("username") or "").strip()
        password = request.data.get("password") or ""

        if not username or not password:
            return Response(
                {"detail": "Please provide both username and password."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = authenticate(request, username=username, password=password)

        # Operators are used to signing in with an email address. Fall back to a
        # unique email match before rejecting the attempt.
        if user is None and "@" in username:
            from django.contrib.auth import get_user_model

            candidates = get_user_model().objects.filter(email__iexact=username)
            if candidates.count() == 1:
                user = authenticate(
                    request, username=candidates.first().username, password=password
                )

        if user is None:
            return Response(
                {"detail": "Invalid credentials."}, status=status.HTTP_401_UNAUTHORIZED
            )
        if not user.is_active:
            return Response(
                {"detail": "This account is disabled."}, status=status.HTTP_403_FORBIDDEN
            )

        login(request, user)
        payload = describe(user)
        payload["message"] = "Login successful"
        return Response(payload, status=status.HTTP_200_OK)


class LogoutView(APIView):
    """Idempotent: logging out when already logged out is not an error."""

    permission_classes = [AllowAny]

    def post(self, request):
        logout(request)
        return Response({"message": "Logged out."}, status=status.HTTP_200_OK)


@method_decorator(ensure_csrf_cookie, name="dispatch")
class SessionView(APIView):
    """Who am I. Lets the SPA restore a session after a browser refresh."""

    permission_classes = [AllowAny]

    def get(self, request):
        if not request.user.is_authenticated:
            return Response({"authenticated": False}, status=status.HTTP_200_OK)
        payload = describe(request.user)
        payload["authenticated"] = True
        return Response(payload, status=status.HTTP_200_OK)


class PasswordChangeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        current = request.data.get("current_password") or ""
        new = request.data.get("new_password") or ""
        if not request.user.check_password(current):
            return Response(
                {"detail": "Current password is incorrect."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(new) < 8:
            return Response(
                {"detail": "New password must be at least eight characters."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        request.user.set_password(new)
        request.user.save(update_fields=["password"])
        login(request, request.user)
        return Response({"message": "Password updated."})


class ClientListView(APIView):
    """
    The clients this user may act for.

    The Login screen used to offer a hard-coded list of health plans and store
    the pick in localStorage, where it did nothing but decorate the header. The
    list comes from ClientMembership now, so a name the browser has not been
    granted is not in the list and would be refused anyway if it sent one.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        selected = resolve_client(request)
        return Response(
            {
                "clients": [
                    {"id": c.id, "code": c.code, "name": c.name}
                    for c in user_clients(request.user)
                ],
                "selected": (
                    {"id": selected.id, "code": selected.code, "name": selected.name}
                    if selected
                    else None
                ),
            }
        )
