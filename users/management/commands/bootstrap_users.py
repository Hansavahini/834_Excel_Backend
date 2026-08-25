"""
Create the two accounts the portal expects on a fresh database.

The front end offers an Admin tab and a Client tab. Those map onto Django's
is_staff flag, not onto a role the browser chooses for itself, so the accounts
have to exist before either tab can be used.
"""

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from users.models import Client, ClientMembership


class Command(BaseCommand):
    help = "Create or update the default admin and client accounts."

    def add_arguments(self, parser):
        parser.add_argument("--admin-username", default=os.environ.get("PORTAL_ADMIN_USER", "admin"))
        parser.add_argument("--admin-password", default=os.environ.get("PORTAL_ADMIN_PASSWORD", "admin12345"))
        parser.add_argument("--admin-email", default=os.environ.get("PORTAL_ADMIN_EMAIL", "admin@onesmarter.local"))
        parser.add_argument("--client-username", default=os.environ.get("PORTAL_CLIENT_USER", "client"))
        parser.add_argument("--client-password", default=os.environ.get("PORTAL_CLIENT_PASSWORD", "client12345"))
        parser.add_argument("--client-email", default=os.environ.get("PORTAL_CLIENT_EMAIL", "client@abchealth.com"))
        parser.add_argument(
            "--plan-name",
            default=os.environ.get("PORTAL_PLAN_NAME", "ABC Health Plan"),
            help="Health plan both accounts are given a membership for.",
        )
        parser.add_argument(
            "--plan-code",
            default=os.environ.get("PORTAL_PLAN_CODE", "abc-health"),
        )

    def handle(self, *args, **options):
        model = get_user_model()

        admin, created = model.objects.get_or_create(
            username=options["admin_username"],
            defaults={"email": options["admin_email"]},
        )
        admin.email = options["admin_email"]
        admin.is_staff = True
        admin.is_superuser = True
        admin.set_password(options["admin_password"])
        admin.save()
        self.stdout.write(
            self.style.SUCCESS(
                "{verb} admin '{u}'".format(verb="Created" if created else "Updated", u=admin.username)
            )
        )

        client, created = model.objects.get_or_create(
            username=options["client_username"],
            defaults={"email": options["client_email"]},
        )
        client.email = options["client_email"]
        client.is_staff = False
        client.is_superuser = False
        client.set_password(options["client_password"])
        client.save()
        self.stdout.write(
            self.style.SUCCESS(
                "{verb} client '{u}'".format(verb="Created" if created else "Updated", u=client.username)
            )
        )
        # Tenancy is not optional once the models carry a client column: an
        # account with no membership can act for no plan, so a fresh install
        # would sign in successfully and then show an empty portal. The
        # backfill migration only covers users that already existed, so
        # accounts created here need their membership made explicitly.
        plan, plan_created = Client.objects.get_or_create(
            code=options["plan_code"],
            defaults={"name": options["plan_name"]},
        )
        self.stdout.write(
            self.style.SUCCESS(
                "{verb} client organisation '{n}'".format(
                    verb="Created" if plan_created else "Found", n=plan.name
                )
            )
        )

        for account in (admin, client):
            membership, made = ClientMembership.objects.get_or_create(
                user=account,
                client=plan,
                defaults={"is_default": not account.client_memberships.exists()},
            )
            self.stdout.write(
                "  {verb} membership {u} -> {c}".format(
                    verb="added" if made else "kept", u=account.username, c=plan.name
                )
            )

        self.stdout.write(
            "The client account can sign in with either the username or the email address."
        )
