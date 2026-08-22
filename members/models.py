"""
members app — people, their coverage spans, and their appearance in daily files.

Design note on subscribers and dependents. The original sketch had a thin
Dependent table hanging off Member. That cannot carry an 834 correctly: in the
005010X220A1 guide every dependent gets its own INS loop with its own
maintenance codes, its own DTP dates and, under most sponsors, its own member
identifier. Dependents term and reinstate independently of the subscriber. A
child table with five name fields has nowhere to put any of that, and it means
MemberEligibilityHistory can only ever describe subscribers.

So Member is the person table for both roles, distinguished by member_type and
linked by a self-referencing subscriber foreign key. The flat Excel row is a
join, not a table shape. Everything downstream — history, daily status, the
SUB/DEP column — falls out of that without special cases.
"""

import hashlib
import hmac

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models

from files.db_compat import check_constraint
from files.models import UploadedFile

SSN_DIGITS = RegexValidator(r"^\d{9}$", "Store SSN as nine digits with no dashes.")
ZIP_CODE = RegexValidator(r"^\d{5}(\d{4})?$", "ZIP is five or nine digits, no dash.")
STATE_CODE = RegexValidator(r"^[A-Z]{2}$", "Two letter uppercase state or territory code.")
PHONE_DIGITS = RegexValidator(r"^\d{10}$", "Store phone as ten digits with no punctuation.")


def ssn_fingerprint(ssn):
    """
    Keyed digest used for identity matching so no query ever needs the plaintext.
    The pepper lives in settings, not in the database, so a stolen database file
    does not yield a rainbow table of nine digit numbers.
    """
    if not ssn:
        return ""
    pepper = settings.SECRET_KEY.encode()
    return hmac.new(pepper, ssn.encode(), hashlib.sha256).hexdigest()


class MemberType(models.TextChoices):
    SUBSCRIBER = "SUB", "Subscriber"
    DEPENDENT = "DEP", "Dependent"


class RelationshipCode(models.TextChoices):
    """INS02 individual relationship code, trimmed to the values a sponsor actually sends."""

    SPOUSE = "01", "Spouse"
    CHILD = "19", "Child"
    SELF = "18", "Self"
    STEPCHILD = "17", "Stepson or stepdaughter"
    FOSTER_CHILD = "10", "Foster child"
    WARD = "15", "Ward"
    LIFE_PARTNER = "53", "Life partner"
    OTHER = "G8", "Other relationship"


class GenderCode(models.TextChoices):
    MALE = "M", "Male"
    FEMALE = "F", "Female"
    UNKNOWN = "U", "Unknown"


class CoverageStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    TERMINATED = "TERMINATED", "Terminated"
    COBRA = "COBRA", "COBRA"
    SURVIVING = "SURVIVING", "Surviving insured"
    UNKNOWN = "UNKNOWN", "Unknown"


class Member(models.Model):
    """One person, subscriber or dependent, as most recently known."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="members"
    )
    member_type = models.CharField(max_length=3, choices=MemberType.choices, db_index=True)
    subscriber = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="dependents",
        help_text="Null on a subscriber row, set on every dependent row.",
    )
    relationship_code = models.CharField(
        max_length=2, choices=RelationshipCode.choices, default=RelationshipCode.SELF
    )

    member_id = models.CharField(max_length=80, blank=True, help_text="Carrier or sponsor assigned identifier, NM109 of the insured loop.")
    subscriber_number = models.CharField(
        max_length=80,
        blank=True,
        help_text=(
            "REF*0F subscriber number, carried on dependents as well. Named subscriber_number "
            "rather than subscriber_id because the self foreign key already owns that attribute."
        ),
    )
    group_number = models.CharField(max_length=50, blank=True, help_text="REF*1L policy or group number.")

    first_name = models.CharField(max_length=35)
    middle_name = models.CharField(max_length=25, blank=True)
    last_name = models.CharField(max_length=60)
    name_suffix = models.CharField(max_length=10, blank=True)

    ssn = models.CharField(
        max_length=9,
        blank=True,
        validators=[SSN_DIGITS],
        help_text=(
            "Plaintext today for parity with the source file. Move this to an application layer "
            "encrypted field or a token before production; see the design note on PHI at rest."
        ),
    )
    ssn_fingerprint = models.CharField(
        max_length=64,
        blank=True,
        db_index=True,
        help_text="Keyed HMAC of the SSN. Match on this, never on the plaintext column.",
    )

    gender_code = models.CharField(max_length=1, choices=GenderCode.choices, default=GenderCode.UNKNOWN)
    date_of_birth = models.DateField(null=True, blank=True)
    date_of_death = models.DateField(null=True, blank=True)

    local = models.CharField(max_length=30, blank=True, help_text="Union local or division, mapped per sponsor.")
    plan_code = models.CharField(max_length=30, blank=True)
    class_code = models.CharField(
        max_length=30,
        blank=True,
        db_column="class_code",
        help_text="The Excel column is CLASS. 'class' is a Python keyword, so the attribute is class_code.",
    )

    address1 = models.CharField(max_length=55, blank=True)
    address2 = models.CharField(max_length=55, blank=True)
    city = models.CharField(max_length=30, blank=True)
    state = models.CharField(max_length=2, blank=True, validators=[STATE_CODE])
    postal_code = models.CharField(max_length=9, blank=True, validators=[ZIP_CODE])
    phone = models.CharField(max_length=10, blank=True, validators=[PHONE_DIGITS])
    email = models.EmailField(max_length=254, blank=True)

    coverage_status = models.CharField(
        max_length=12,
        choices=CoverageStatus.choices,
        default=CoverageStatus.UNKNOWN,
        db_index=True,
        help_text="Denormalised from the open eligibility span. Recomputed on write, never edited by hand.",
    )
    benefit_status_code = models.CharField(max_length=2, blank=True, help_text="INS05.")
    employment_status_code = models.CharField(max_length=2, blank=True, help_text="INS08.")
    student_status_code = models.CharField(max_length=1, blank=True, help_text="INS09.")

    first_seen_file = models.ForeignKey(
        UploadedFile, null=True, blank=True, on_delete=models.PROTECT, related_name="members_first_seen"
    )
    last_seen_file = models.ForeignKey(
        UploadedFile, null=True, blank=True, on_delete=models.PROTECT, related_name="members_last_seen"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("last_name", "first_name")
        indexes = [
            models.Index(fields=["owner", "last_name", "first_name"], name="mem_owner_name_idx"),
            models.Index(fields=["owner", "member_type", "coverage_status"], name="mem_owner_type_status_idx"),
            models.Index(fields=["subscriber", "member_type"], name="mem_subscriber_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "member_id"],
                condition=~models.Q(member_id=""),
                name="uniq_member_id_per_owner",
            ),
            check_constraint(
                condition=(models.Q(member_type="SUB") & models.Q(subscriber__isnull=True))
                | (models.Q(member_type="DEP") & models.Q(subscriber__isnull=False)),
                name="dependent_requires_subscriber",
                violation_error_message="A subscriber row has no subscriber link, a dependent row must have one.",
            ),
            check_constraint(
                condition=models.Q(date_of_death__isnull=True)
                | models.Q(date_of_birth__isnull=True)
                | models.Q(date_of_death__gte=models.F("date_of_birth")),
                name="death_not_before_birth",
            ),
        ]

    def save(self, *args, **kwargs):
        self.ssn_fingerprint = ssn_fingerprint(self.ssn)
        super().save(*args, **kwargs)

    @property
    def full_name(self):
        return " ".join(part for part in (self.first_name, self.middle_name, self.last_name) if part)

    @property
    def masked_ssn(self):
        return "XXX-XX-{last4}".format(last4=self.ssn[-4:]) if self.ssn else ""

    def __str__(self):
        return "{name} [{kind}]".format(name=self.full_name, kind=self.member_type)


class CustodialParent(models.Model):
    """
    The custodial parent block from the Excel layout. Kept out of Member because
    it applies to a minority of dependents and would otherwise add seven columns
    that are null on every subscriber row.
    """

    member = models.OneToOneField(Member, on_delete=models.CASCADE, related_name="custodial_parent")
    first_name = models.CharField(max_length=35, blank=True)
    last_name = models.CharField(max_length=60, blank=True)
    address1 = models.CharField(max_length=55, blank=True)
    address2 = models.CharField(max_length=55, blank=True)
    city = models.CharField(max_length=30, blank=True)
    state = models.CharField(max_length=2, blank=True, validators=[STATE_CODE])
    postal_code = models.CharField(max_length=9, blank=True, validators=[ZIP_CODE])
    phone = models.CharField(max_length=10, blank=True, validators=[PHONE_DIGITS])

    def __str__(self):
        return "Custodial parent of {member}".format(member=self.member_id)


class MemberEligibilityHistory(models.Model):
    """
    One coverage span. Multiple rows per member, never numbered columns, and
    never deleted. A member with medical and dental on different dates has one
    row per line of business, which is why insurance_line_code is part of the key.
    """

    class MaintenanceType(models.TextChoices):
        CHANGE = "001", "Change"
        ADDITION = "021", "Addition"
        CANCELLATION = "024", "Cancellation or termination"
        REINSTATEMENT = "025", "Reinstatement"
        AUDIT = "030", "Audit or compare"

    member = models.ForeignKey(
        Member, on_delete=models.PROTECT, related_name="eligibility_history"
    )
    insurance_line_code = models.CharField(
        max_length=3, default="HLT", help_text="HD03, e.g. HLT medical, DEN dental, VIS vision."
    )
    plan_code = models.CharField(max_length=30, blank=True)
    class_code = models.CharField(max_length=30, blank=True)
    effective_date = models.DateField()
    termination_date = models.DateField(
        null=True, blank=True, help_text="Null means the span is open."
    )
    maintenance_type_code = models.CharField(
        max_length=3, choices=MaintenanceType.choices, blank=True, help_text="INS03."
    )
    maintenance_reason_code = models.CharField(max_length=3, blank=True, help_text="INS04.")
    source_file = models.ForeignKey(
        UploadedFile, on_delete=models.PROTECT, related_name="eligibility_rows"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "member eligibility history"
        ordering = ("member_id", "insurance_line_code", "-effective_date")
        indexes = [
            models.Index(fields=["member", "insurance_line_code", "-effective_date"], name="elig_member_line_idx"),
            models.Index(fields=["termination_date"], name="elig_open_span_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["member", "insurance_line_code", "effective_date"],
                name="uniq_span_start_per_member_line",
            ),
            check_constraint(
                condition=models.Q(termination_date__isnull=True)
                | models.Q(termination_date__gte=models.F("effective_date")),
                name="term_not_before_effective",
                violation_error_message="Termination date cannot precede the effective date.",
            ),
        ]

    @property
    def is_open(self):
        return self.termination_date is None

    def __str__(self):
        return "{start} to {end}".format(
            start=self.effective_date, end=self.termination_date or "open"
        )


class MemberDailyStatus(models.Model):
    """
    Presence of a member in one day's file, and what changed.

    Only rows for members that appeared are written. Absence is derived by
    anti-join against the roster of members known on that date, which keeps the
    table proportional to file contents rather than to members multiplied by
    days. A 6,000 member sponsor filed daily would otherwise generate about
    2.2 million rows a year of which the overwhelming majority say nothing.
    """

    class ChangeType(models.TextChoices):
        ADDED = "ADDED", "New this file"
        UNCHANGED = "UNCHANGED", "Present, no change"
        CHANGED = "CHANGED", "Present, demographic or plan change"
        REINSTATED = "REINSTATED", "Present, coverage reopened"
        TERMINATED = "TERMINATED", "Present, coverage closed"

    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="daily_statuses")
    uploaded_file = models.ForeignKey(
        UploadedFile, on_delete=models.PROTECT, related_name="daily_statuses"
    )
    status_date = models.DateField(db_index=True, help_text="Copied from UploadedFile.file_date at write time.")
    change_type = models.CharField(max_length=12, choices=ChangeType.choices, default=ChangeType.UNCHANGED)
    changed_fields = models.JSONField(
        default=dict, blank=True, help_text='Field level diff, e.g. {"plan_code": ["PPO", "HDHP"]}.'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "member daily statuses"
        ordering = ("-status_date", "member_id")
        indexes = [
            models.Index(fields=["status_date", "change_type"], name="mds_date_change_idx"),
            models.Index(fields=["member", "-status_date"], name="mds_member_date_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["member", "status_date"], name="uniq_member_status_per_date"
            )
        ]

    def __str__(self):
        return "{member} present on {date}".format(member=self.member_id, date=self.status_date)
