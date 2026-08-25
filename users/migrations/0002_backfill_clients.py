"""
Give an existing install a tenancy without changing who can see what.

Before this migration everything was scoped on owner alone. The safest possible
starting point for client scoping is therefore one client per existing user:
after the backfill each user sees exactly the rows they saw before, and an
operator can then merge users onto a shared client deliberately rather than
discovering the merge by accident.

Runs after every app that gained a client column, so the backfill can fill them
all in one pass. Reversible: the reverse simply clears the columns and drops the
generated clients, leaving owner scoping as it was.
"""

from django.db import migrations


def slugify_username(username, taken):
    base = "".join(ch if ch.isalnum() else "-" for ch in username.lower()).strip("-")
    base = (base or "client")[:26]
    code = base
    suffix = 1
    while code in taken:
        suffix += 1
        code = "{base}-{n}".format(base=base[:24], n=suffix)
    taken.add(code)
    return code


def forwards(apps, schema_editor):
    User = apps.get_model("auth", "User")
    Client = apps.get_model("users", "Client")
    ClientMembership = apps.get_model("users", "ClientMembership")

    scoped = [
        ("files", "UploadedFile"),
        ("files", "GeneratedFile"),
        ("mapping", "MappingTemplate"),
        ("conversion", "ConversionHistory"),
        ("members", "Member"),
    ]

    taken = set(Client.objects.values_list("code", flat=True))
    client_for_user = {}

    for user in User.objects.all().order_by("id"):
        existing = (
            ClientMembership.objects.filter(user=user).select_related("client").first()
        )
        if existing:
            client_for_user[user.id] = existing.client
            continue

        display = (user.get_full_name() if hasattr(user, "get_full_name") else "") or user.username
        client = Client.objects.create(
            code=slugify_username(user.username, taken),
            name="{name} (migrated)".format(name=display)[:120],
            is_active=True,
        )
        ClientMembership.objects.create(user=user, client=client, is_default=True)
        client_for_user[user.id] = client

    for app_label, model_name in scoped:
        model = apps.get_model(app_label, model_name)
        for owner_id, client in client_for_user.items():
            model.objects.filter(owner_id=owner_id, client__isnull=True).update(
                client=client
            )


def backwards(apps, schema_editor):
    Client = apps.get_model("users", "Client")
    ClientMembership = apps.get_model("users", "ClientMembership")

    for app_label, model_name in (
        ("members", "Member"),
        ("conversion", "ConversionHistory"),
        ("mapping", "MappingTemplate"),
        ("files", "GeneratedFile"),
        ("files", "UploadedFile"),
    ):
        apps.get_model(app_label, model_name).objects.update(client=None)

    ClientMembership.objects.all().delete()
    Client.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0001_initial"),
        ("files", "0002_remove_uploadedfile_uniq_upload_per_owner_checksum_and_more"),
        ("mapping", "0002_remove_mappingtemplate_uniq_template_name_version_per_owner_and_more"),
        ("conversion", "0002_conversionhistory_client"),
        ("members", "0004_remove_member_uniq_member_id_per_owner_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
