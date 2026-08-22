"""
Idempotent seed of the SegmentElement dictionary that backs the mapping dropdowns.

Caveat worth taking seriously: these entries are drawn from working knowledge of
the X12N 834 005010X220A1 implementation guide, not from a machine reading of it.
Before this list goes in front of a client, have someone diff it against your
licensed copy of the TR3. Element positions are the sort of thing that is easy to
get almost right, and a mapping built on an almost-right dictionary fails quietly.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from mapping.models import SegmentElement

# (segment, element, description, loop, data type)
ELEMENTS = [
    ("INS", "INS01", "Member indicator, Y subscriber / N dependent", "2000", "ID"),
    ("INS", "INS02", "Individual relationship code", "2000", "ID"),
    ("INS", "INS03", "Maintenance type code", "2000", "ID"),
    ("INS", "INS04", "Maintenance reason code", "2000", "ID"),
    ("INS", "INS05", "Benefit status code", "2000", "ID"),
    ("REF", "REF01", "Reference identification qualifier", "2000", "ID"),
    ("REF", "REF02", "Reference identification value", "2000", "AN"),
    ("NM1", "NM101", "Entity identifier code", "2100A", "ID"),
    ("NM1", "NM102", "Entity type qualifier", "2100A", "ID"),
    ("NM1", "NM103", "Last name or organisation name", "2100A", "AN"),
    ("NM1", "NM104", "First name", "2100A", "AN"),
    ("NM1", "NM105", "Middle name", "2100A", "AN"),
    ("NM1", "NM107", "Name suffix", "2100A", "AN"),
    ("NM1", "NM108", "Identification code qualifier", "2100A", "ID"),
    ("NM1", "NM109", "Identification code", "2100A", "AN"),
    ("PER", "PER01", "Contact function code", "2100A", "ID"),
    ("PER", "PER03", "Communication number qualifier", "2100A", "ID"),
    ("PER", "PER04", "Communication number", "2100A", "AN"),
    ("PER", "PER05", "Communication number qualifier, second", "2100A", "ID"),
    ("PER", "PER06", "Communication number, second", "2100A", "AN"),
    ("N3", "N301", "Address line one", "2100A", "AN"),
    ("N3", "N302", "Address line two", "2100A", "AN"),
    ("N4", "N401", "City name", "2100A", "AN"),
    ("N4", "N402", "State or province code", "2100A", "ID"),
    ("N4", "N403", "Postal code", "2100A", "ID"),
    ("N4", "N404", "Country code", "2100A", "ID"),
    ("DMG", "DMG01", "Date time period format qualifier", "2100A", "ID"),
    ("DMG", "DMG02", "Date of birth", "2100A", "DT"),
    ("DMG", "DMG03", "Gender code", "2100A", "ID"),
    ("DMG", "DMG04", "Marital status code", "2100A", "ID"),
    ("DMG", "DMG05", "Citizenship status code", "2100A", "ID"),
    ("DMG", "DMG06", "Country code", "2100A", "ID"),
    ("DTP", "DTP01", "Date time qualifier", "2000", "ID"),
    ("DTP", "DTP02", "Date time period format qualifier", "2000", "ID"),
    ("DTP", "DTP03", "Date time period value", "2000", "AN"),
    ("HD", "HD01", "Maintenance type code", "2300", "ID"),
    ("HD", "HD03", "Insurance line code", "2300", "ID"),
    ("HD", "HD04", "Plan coverage description", "2300", "AN"),
    ("HD", "HD05", "Coverage level code", "2300", "ID"),
    ("LUI", "LUI02", "Language code", "2100A", "AN"),
    ("BGN", "BGN02", "Transaction set reference number", "header", "AN"),
    ("BGN", "BGN03", "Transaction set creation date", "header", "DT"),
    ("BGN", "BGN08", "Action code, 2 change / 4 verify / RX replace", "header", "ID"),
]


class Command(BaseCommand):
    help = "Seed or refresh the 834 segment and element dictionary."

    @transaction.atomic
    def handle(self, *args, **options):
        created = updated = 0
        for segment, element, description, loop, data_type in ELEMENTS:
            obj, was_created = SegmentElement.objects.update_or_create(
                segment_name=segment,
                element_code=element,
                loop_id=loop,
                defaults={"description": description, "data_type": data_type, "is_active": True},
            )
            created += was_created
            updated += not was_created
        self.stdout.write(
            self.style.SUCCESS("Segment dictionary seeded: {c} created, {u} refreshed.".format(c=created, u=updated))
        )
