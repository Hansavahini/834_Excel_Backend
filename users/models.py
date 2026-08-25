"""
Client tenancy.

The Login screen has always offered a list of health plans — ABC Health Plan,
CareFirst, Horizon — and the choice was written to localStorage and used for
nothing but a label in the header. Every query in the project scoped on
owner=request.user alone, so the client name the browser sent was never checked
against anything and could not isolate anything.

This is the smallest structure that makes the choice real: a Client, an explicit
membership table saying which users may act for which client, and a nullable
client column on every record that carries plan data. Nullable because existing
rows predate the column; the accompanying data migration gives each existing
owner a client of their own, so isolation after the migration is at least as
tight as it was before it.
"""

from django.conf import settings
from django.db import models


class Client(models.Model):
    """One health plan, TPA or sponsor whose data must not mix with another's."""

    code = models.SlugField(
        max_length=32,
        unique=True,
        help_text="Stable short key used in APIs, e.g. abc-health. Never renamed.",
    )
    name = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name


class ClientMembership(models.Model):
    """
    Which users may act for which client.

    Authorisation lives here, not in the request body. A browser may say which
    client it wants; whether it gets it is decided by a row in this table.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="client_memberships",
    )
    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name="memberships"
    )
    is_default = models.BooleanField(
        default=False,
        help_text="Selected automatically when the browser names no client.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("user_id", "client__name")
        constraints = [
            models.UniqueConstraint(
                fields=["user", "client"], name="uniq_client_membership"
            ),
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(is_default=True),
                name="one_default_client_per_user",
            ),
        ]

    def __str__(self):
        return "{user} -> {client}".format(user=self.user_id, client=self.client_id)
