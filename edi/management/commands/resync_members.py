"""
Re-run the member sync over uploads that are already stored.

Useful after a fix to the sync engine, and after restoring a database whose
members tables were built by an older version of the pipeline.
"""

from django.core.management.base import BaseCommand

from edi.services.ingest import sync_uploaded_file
from files.models import UploadedFile


class Command(BaseCommand):
    help = "Re-parse stored 834 files and reconcile the members tables."

    def add_arguments(self, parser):
        parser.add_argument("--file-id", type=int, help="Only this uploaded file.")
        parser.add_argument("--owner", type=str, help="Only files owned by this username.")
        parser.add_argument(
            "--include-failed",
            action="store_true",
            help="Also re-run files whose parse previously failed.",
        )

    def handle(self, *args, **options):
        queryset = UploadedFile.objects.select_related("owner").order_by("file_date", "id")
        if options.get("file_id"):
            queryset = queryset.filter(pk=options["file_id"])
        if options.get("owner"):
            queryset = queryset.filter(owner__username=options["owner"])
        if not options.get("include_failed"):
            queryset = queryset.exclude(processing_status="FAILED")

        if not queryset.exists():
            self.stdout.write(self.style.WARNING("Nothing to re-sync."))
            return

        for record in queryset:
            try:
                summary = sync_uploaded_file(record, record.owner)
            except Exception as exc:  # noqa: BLE001
                self.stdout.write(
                    self.style.ERROR(
                        "{name}: {kind}: {exc}".format(
                            name=record.original_filename, kind=type(exc).__name__, exc=exc
                        )
                    )
                )
                continue
            self.stdout.write(
                self.style.SUCCESS(
                    "{name} ({date}): {ok}/{total} loops synced, {bad} failed".format(
                        name=record.original_filename,
                        date=record.file_date or "undated",
                        ok=summary["synced"],
                        total=summary["loops"],
                        bad=summary["failed"],
                    )
                )
            )
