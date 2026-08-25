"""
Remove plaintext SSNs, keeping the fingerprint and the last four digits.

Why this is a command and not a migration. Dropping the plaintext is the right
end state — every screen in this application displays a mask, and every lookup
matches on the HMAC fingerprint, so the column has no consumer left once
ssn_last4 is populated. But it is irreversible, and it forecloses one thing:
rotating SSN_PEPPER requires re-deriving every fingerprint from the plaintext,
so once the plaintext is gone the pepper is fixed for the life of the data.

That is a decision for whoever owns the deployment, not for a migration that
runs itself during a deploy. So it is an explicit, deliberate step with a dry
run, a required confirmation, and a refusal to proceed if the derived columns
are not in place.

    python manage.py purge_plaintext_ssn --dry-run
    python manage.py purge_plaintext_ssn --confirm
"""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from members.models import Member


class Command(BaseCommand):
    help = "Clear Member.ssn, retaining ssn_fingerprint and ssn_last4."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change and exit without writing.",
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Required to actually clear the column. Irreversible.",
        )

    def handle(self, *args, **options):
        populated = Member.objects.exclude(ssn="")
        total = populated.count()

        if total == 0:
            self.stdout.write(self.style.SUCCESS("No plaintext SSNs are stored."))
            return

        # Refuse to destroy the source before the derived values exist, or the
        # masks and the identity matching go with it.
        unprepared = populated.filter(Q(ssn_fingerprint="") | Q(ssn_last4="")).count()
        if unprepared:
            raise CommandError(
                "{n} of {t} members are missing a fingerprint or last-four value. "
                "Run migrations first; purging now would lose the ability to match "
                "or display those members at all.".format(n=unprepared, t=total)
            )

        self.stdout.write(
            "{t} members carry a plaintext SSN. All {t} have a fingerprint and "
            "last-four value already.".format(t=total)
        )

        if options["dry_run"]:
            self.stdout.write(
                self.style.WARNING("Dry run: nothing was written.")
            )
            return

        if not options["confirm"]:
            raise CommandError(
                "Refusing to purge without --confirm. This cannot be undone, and it "
                "permanently prevents rotating SSN_PEPPER, which needs the plaintext "
                "to re-derive fingerprints."
            )

        # update() rather than save(): the model's save() re-derives the
        # fingerprint from self.ssn, and with the plaintext blanked that would
        # be a no-op at best. A direct update leaves the derived columns alone.
        updated = populated.update(ssn="")

        self.stdout.write(
            self.style.SUCCESS(
                "Cleared the plaintext SSN on {n} members. Fingerprints and "
                "last-four values are intact; SSN_PEPPER can no longer be "
                "rotated for this data.".format(n=updated)
            )
        )
