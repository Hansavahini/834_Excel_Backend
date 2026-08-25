"""
Idempotent seed of the SegmentElement dictionary that backs the mapping dropdowns.

Issue 6.4: the dropdowns were a literal inside a React component, so the set of
segments anyone could map was the nine somebody had typed into the front end,
while this table sat seeded and migrated with the rest of the schema. Adding a
segment meant a front-end release. The API serves this table now, so adding one
is a row — and the list below has been widened to cover the segments an 834
actually carries rather than the handful the UI happened to show.

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
    ("INS", "INS06", "Medicare plan code", "2000", "ID"),
    ("INS", "INS08", "Employment status code", "2000", "ID"),
    ("INS", "INS09", "Student status code", "2000", "ID"),
    ("INS", "INS10", "Handicap indicator", "2000", "ID"),
    ("INS", "INS17", "Birth sequence number", "2000", "N0"),
    ("REF", "REF03", "Description", "2000", "AN"),
    ("NM1", "NM106", "Name prefix", "2100A", "AN"),
    ("NM1", "NM110", "Entity relationship code", "2100A", "ID"),
    ("NM1", "NM111", "Entity identifier code, secondary", "2100A", "ID"),
    ("PER", "PER02", "Contact name", "2100A", "AN"),
    ("PER", "PER07", "Communication number qualifier, third", "2100A", "ID"),
    ("PER", "PER08", "Communication number, third", "2100A", "AN"),
    ("N4", "N405", "Location qualifier", "2100A", "ID"),
    ("N4", "N406", "Location identifier", "2100A", "AN"),
    ("DMG", "DMG10", "Race or ethnicity, composite", "2100A", "AN"),
    ("DMG", "DMG11", "Citizenship status code", "2100A", "ID"),
    ("HD", "HD02", "Maintenance reason code", "2300", "ID"),
    ("HD", "HD06", "Description", "2300", "AN"),
    ("HD", "HD07", "Late enrollment indicator", "2300", "ID"),
    ("HD", "HD09", "Drug house indicator", "2300", "ID"),
    ("HD", "HD11", "Late enrolment indicator", "2300", "ID"),
    ("LX", "LX01", "Assigned number", "2700", "N0"),
    ("LS", "LS01", "Loop identifier code", "2700", "AN"),
    ("LE", "LE01", "Loop identifier code", "2700", "AN"),
    ("AMT", "AMT01", "Amount qualifier code", "2300", "ID"),
    ("AMT", "AMT02", "Monetary amount", "2300", "R"),
    ("COB", "COB01", "Payer responsibility sequence number", "2320", "ID"),
    ("COB", "COB02", "Reference identification, policy number", "2320", "AN"),
    ("COB", "COB03", "Coordination of benefits code", "2320", "ID"),
    ("HLH", "HLH01", "Health related code", "2100A", "ID"),
    ("HLH", "HLH02", "Height in inches", "2100A", "R"),
    ("HLH", "HLH03", "Weight in pounds", "2100A", "R"),
    ("ICM", "ICM01", "Frequency code", "2100A", "ID"),
    ("ICM", "ICM02", "Wage amount", "2100A", "R"),
    ("IDC", "IDC01", "Plan coverage description", "2300", "AN"),
    ("IDC", "IDC02", "Identification card type code", "2300", "ID"),
    ("N1", "N101", "Entity identifier code", "1000A", "ID"),
    ("N1", "N102", "Sponsor or payer name", "1000A", "AN"),
    ("N1", "N103", "Identification code qualifier", "1000A", "ID"),
    ("N1", "N104", "Identification code", "1000A", "AN"),
    ("EC", "EC01", "Employment class code", "2100A", "ID"),
    ("LM", "LM01", "Agency qualifier code", "2100A", "ID"),
    ("LUI", "LUI01", "Identification code qualifier", "2100A", "ID"),
    ("LUI", "LUI03", "Description", "2100A", "AN"),
    ("LUI", "LUI04", "Use of language indicator", "2100A", "ID"),
    ("BGN", "BGN01", "Transaction set purpose code", "header", "ID"),
    ("BGN", "BGN04", "Transaction set creation time", "header", "TM"),
    ("BGN", "BGN05", "Time zone code", "header", "ID"),
    ("BGN", "BGN06", "Original reference number", "header", "AN"),
    ("DTP", "DTP04", "Date time period, secondary", "2000", "AN"),
    ("QTY", "QTY01", "Quantity qualifier", "2000", "ID"),
    ("QTY", "QTY02", "Quantity", "2000", "R"),
    ("ST", "ST01", "Transaction set identifier code", "header", "ID"),
    ("ST", "ST02", "Transaction set control number", "header", "AN"),
    ("ST", "ST03", "Implementation convention reference", "header", "AN"),
    ("GS", "GS02", "Application sender code", "envelope", "AN"),
    ("GS", "GS03", "Application receiver code", "envelope", "AN"),
    ("GS", "GS04", "Group date", "envelope", "DT"),
    ("ISA", "ISA06", "Interchange sender id", "envelope", "AN"),
    ("ISA", "ISA08", "Interchange receiver id", "envelope", "AN"),
    ("ISA", "ISA13", "Interchange control number", "envelope", "N0"),
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
