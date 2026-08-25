"""
Derive ssn_last4 from the plaintext column while it is still populated.

Runs once, before any operator chooses to purge the plaintext. After the purge
the last four digits and the fingerprint are all that remain, and they are all
the application ever needed: every screen displays a mask, and every lookup
matches on the fingerprint.
"""

from django.db import migrations


def forwards(apps, schema_editor):
    Member = apps.get_model("members", "Member")
    for member in Member.objects.exclude(ssn="").only("id", "ssn").iterator():
        digits = "".join(ch for ch in (member.ssn or "") if ch.isdigit())
        if len(digits) >= 4:
            Member.objects.filter(pk=member.pk).update(ssn_last4=digits[-4:])


def backwards(apps, schema_editor):
    # The column goes away with the schema migration; nothing to undo, and the
    # plaintext it was derived from is untouched by this migration.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("members", "0005_add_ssn_last4"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
