"""
Resolve which client a request is acting for, and prove the user may.

The rule this module exists to enforce: the browser proposes, the database
disposes. A request may name a client by id or by code, in a header or a query
parameter, and none of that is trusted on its own — the name is looked up
against ClientMembership for the authenticated user, and a client the user has
no membership for is refused rather than quietly ignored.

Every scoped queryset goes through scope_to_client() so there is one place to
read when someone asks whether an endpoint leaks across tenants.
"""

from __future__ import annotations

from typing import Optional

from rest_framework.exceptions import PermissionDenied

from .models import Client, ClientMembership

CLIENT_HEADER = "HTTP_X_CLIENT_ID"


def user_clients(user):
    """Active clients the user is a member of."""
    if not user or not user.is_authenticated:
        return Client.objects.none()
    return Client.objects.filter(
        is_active=True, memberships__user=user
    ).distinct()


def default_client(user) -> Optional[Client]:
    """The membership flagged default, else the only one, else None."""
    membership = (
        ClientMembership.objects.filter(user=user, client__is_active=True)
        .select_related("client")
        .order_by("-is_default", "client__name")
        .first()
    )
    return membership.client if membership else None


def _requested_key(request) -> str:
    return (
        request.META.get(CLIENT_HEADER)
        or request.query_params.get("client_id")
        or request.query_params.get("client")
        or (request.data.get("client_id") if hasattr(request, "data") and isinstance(request.data, dict) else None)
        or ""
    )


def resolve_client(request) -> Optional[Client]:
    """
    The client this request acts for.

    Returns None when the deployment has no clients configured at all, which is
    what an existing single-tenant install looks like before the operator sets
    any up. Raises PermissionDenied when a client is named that the user has no
    membership for — silently falling back to their default would let a probe
    tell real client ids from fake ones by watching the data change.
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return None

    key = str(_requested_key(request)).strip()
    if not key:
        return default_client(user)

    allowed = user_clients(user)
    client = allowed.filter(pk=key).first() if key.isdigit() else None
    if client is None:
        client = allowed.filter(code__iexact=key).first()
    if client is None:
        client = allowed.filter(name__iexact=key).first()

    if client is None:
        raise PermissionDenied(
            "You do not have access to the selected client, or it does not exist."
        )
    return client


def scope_to_client(queryset, client, field: str = "client"):
    """
    Narrow a queryset to one client.

    Rows written before the client column existed carry NULL. Those are
    included alongside the selected client's rows so a migration does not make
    a user's own history disappear from under them; they are still owner-scoped
    by the caller, so this widens visibility by nothing.
    """
    if client is None:
        return queryset
    lookup = {"{f}__in".format(f=field): [client.pk]}
    null_lookup = {"{f}__isnull".format(f=field): True}
    from django.db.models import Q

    return queryset.filter(Q(**lookup) | Q(**null_lookup))
