"""
mapping app — user-defined translation from 834 segments to Excel columns.

The important addition to the original sketch is the qualifier. A bare
(segment, element) pair is ambiguous in an 834: NM1 appears for the insured,
for the sponsor, for the payer and for the custodial parent, and DTP03 is a
begin date, an end date, a hire date or a maintenance date depending entirely
on DTP01. Without a qualifier the mapping cannot be resolved deterministically.
"""

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models

from files.db_compat import check_constraint

SEGMENT_ID = RegexValidator(r"^[A-Z][A-Z0-9]{1,2}$", "Segment id is two or three uppercase alphanumerics, e.g. NM1, DMG, N3.")
ELEMENT_CODE = RegexValidator(r"^[A-Z][A-Z0-9]{1,2}\d{2}$", "Element code is the segment id followed by a two digit position, e.g. NM103.")


class SegmentElement(models.Model):
    """
    Master dictionary that backs the dropdowns. Seeded once from the X12N 834
    005010X220A1 implementation guide, then treated as reference data.
    """

    segment_name = models.CharField(max_length=3, validators=[SEGMENT_ID], db_index=True)
    element_code = models.CharField(max_length=6, validators=[ELEMENT_CODE])
    description = models.CharField(max_length=120)
    loop_id = models.CharField(max_length=8, blank=True, help_text="Implementation guide loop, e.g. 2000, 2100A, 2300.")
    data_type = models.CharField(max_length=12, blank=True, help_text="AN, ID, DT, N0 and so on.")
    max_length = models.PositiveSmallIntegerField(null=True, blank=True)
    is_composite = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ("segment_name", "element_code")
        constraints = [
            models.UniqueConstraint(
                fields=["segment_name", "element_code", "loop_id"], name="uniq_segment_element_loop"
            )
        ]

    def __str__(self):
        return "{code} - {desc}".format(code=self.element_code, desc=self.description)


class MappingTemplate(models.Model):
    """A named, versioned set of column rules belonging to one user."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="mapping_templates"
    )
    client = models.ForeignKey(
        "users.Client",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="mapping_templates",
        help_text="Health plan this record belongs to. Null on rows written before tenancy existed.",
    )
    mapping_name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    version = models.PositiveIntegerField(default=1)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    locked_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Set the moment this version is used by a completed conversion. A locked "
            "version is never edited again; an edit clones it to the next version, so "
            "ConversionHistory.mapping_version keeps meaning what it said at the time."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("owner_id", "mapping_name", "-version")
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "client", "mapping_name", "version"],
                name="uniq_template_name_version_per_owner",
            ),
            models.UniqueConstraint(
                fields=["owner", "client"],
                condition=models.Q(is_default=True),
                name="one_default_template_per_owner",
            ),
        ]

    @property
    def is_locked(self):
        """True once a completed conversion has used this version."""
        return self.locked_at is not None

    def __str__(self):
        return "{name} v{version}".format(name=self.mapping_name, version=self.version)


class MappingDetail(models.Model):
    """One Excel column and the 834 location it is drawn from."""

    class Transform(models.TextChoices):
        NONE = "NONE", "Raw value"
        DATE_MDY = "DATE_MDY", "CCYYMMDD to MM/DD/YYYY"
        DATE_ISO = "DATE_ISO", "CCYYMMDD to YYYY-MM-DD"
        SSN_DASHED = "SSN_DASHED", "9 digits to NNN-NN-NNNN"
        SSN_LAST4 = "SSN_LAST4", "Mask to last four digits"
        UPPER = "UPPER", "Uppercase"
        TITLE = "TITLE", "Title case"
        PHONE = "PHONE", "10 digits to (NNN) NNN-NNNN"

    mapping_template = models.ForeignKey(
        MappingTemplate, on_delete=models.CASCADE, related_name="details"
    )
    excel_column = models.CharField(max_length=64, help_text="Header text as it appears in the workbook, e.g. DEP LAST NAME.")
    column_order = models.PositiveSmallIntegerField()
    segment_element = models.ForeignKey(
        SegmentElement, on_delete=models.PROTECT, related_name="mapping_details"
    )
    qualifier_element = models.CharField(
        max_length=6,
        blank=True,
        help_text="Element that disambiguates the occurrence, e.g. NM101 or DTP01.",
    )
    qualifier_value = models.CharField(
        max_length=12,
        blank=True,
        help_text="Required value of the qualifier element, e.g. IL for the insured, 348 for a benefit begin date.",
    )
    component_index = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="1-based sub-element position when the element is composite."
    )
    occurrence = models.PositiveSmallIntegerField(
        default=1, help_text="Which matching occurrence to take when the segment legitimately repeats."
    )
    applies_to = models.CharField(
        max_length=8,
        choices=[("BOTH", "Subscriber and dependent"), ("SUB", "Subscriber only"), ("DEP", "Dependent only")],
        default="BOTH",
    )
    transform = models.CharField(max_length=12, choices=Transform.choices, default=Transform.NONE)
    default_value = models.CharField(max_length=120, blank=True)
    is_required = models.BooleanField(default=False, help_text="Parser raises a warning when the source element is absent.")

    class Meta:
        ordering = ("mapping_template_id", "column_order")
        constraints = [
            models.UniqueConstraint(
                fields=["mapping_template", "excel_column"], name="uniq_excel_column_per_template"
            ),
            models.UniqueConstraint(
                fields=["mapping_template", "column_order"], name="uniq_column_order_per_template"
            ),
            check_constraint(
                condition=(models.Q(qualifier_element="") & models.Q(qualifier_value=""))
                | (~models.Q(qualifier_element="") & ~models.Q(qualifier_value="")),
                name="qualifier_element_and_value_together",
                violation_error_message="Give both a qualifier element and a qualifier value, or neither.",
            ),
        ]

    @property
    def segment(self):
        return self.segment_element.segment_name

    @property
    def element(self):
        return self.segment_element.element_code

    def __str__(self):
        return "{col} <- {seg}".format(col=self.excel_column, seg=self.element)
