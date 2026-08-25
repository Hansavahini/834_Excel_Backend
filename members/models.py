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


def normalize_ssn(value):
    """
    Reduce an SSN to the digits it is made of.

    Trading partners send 123-45-6789, 123 45 6789 and 123456789 for the same
    person. Fingerprinting the raw string meant those three spellings produced
    three different digests and therefore three duplicate members, which is the
    kind of bug that only shows up once a second sponsor comes on board.
    """
    if not value:
        return ""
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits[:9] if len(digits) >= 9 else digits


def ssn_fingerprint(ssn):
    """
    Keyed digest used for identity matching so no query ever needs the plaintext.
    The pepper lives in settings, not in the database, so a stolen database file
    does not yield a rainbow table of nine digit numbers.

    The pepper is its own setting rather than SECRET_KEY. They were the same
    value, which quietly coupled two unrelated rotation schedules: rotating
    SECRET_KEY is routine — it is what you do after a leaked deployment — and
    doing it here would have re-digested every SSN under a new key, so no stored
    fingerprint would match any incoming member again and identity resolution
    would silently start creating duplicates instead of matching. SSN_PEPPER
    defaults to SECRET_KEY so existing fingerprints keep working; set it
    explicitly and the two can move independently.
    """
    normalized = normalize_ssn(ssn)
    if not normalized:
        return ""
    pepper = getattr(settings, "SSN_PEPPER", None) or settings.SECRET_KEY
    if not isinstance(pepper, bytes):
        pepper = str(pepper).encode()
    return hmac.new(pepper, normalized.encode(), hashlib.sha256).hexdigest()


def ssn_last4_of(ssn):
    """Last four digits, which is all any screen in this application displays."""
    normalized = normalize_ssn(ssn)
    return normalized[-4:] if len(normalized) >= 4 else ""


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
    client = models.ForeignKey(
        "users.Client",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="members",
        help_text="Health plan this record belongs to. Null on rows written before tenancy existed.",
    )
    member_type = models.CharField(max_length=3, choices=MemberType.choices, db_index=True)
    subscriber = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="dependents",
        help_text=(
            "Null on a subscriber row. Set on a dependent row once the subscriber is "
            "known; null with subscriber_pending=True while it is not."
        ),
    )
    subscriber_pending = models.BooleanField(
        default=False,
        db_index=True,
        help_text=(
            "This dependent arrived before its subscriber loop and is waiting to be "
            "linked. The member_type stays DEP; it is never rewritten to SUB to satisfy "
            "a constraint, because the relink pass only looks at DEP rows and a row "
            "flipped to SUB would never come back."
        ),
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
        max_length=50,
        blank=True,
        help_text=(
            "Plaintext today for parity with the source file. Move this to an application layer "
            "encrypted field or a token before production; see the design note on PHI at rest. "
            "Normalised to nine digits on save; SSN_DIGITS is applied in clean() rather than as a "
            "field validator because migration 0002 widened this column and the model had drifted "
            "from it."
        ),
    )
    ssn_fingerprint = models.CharField(
        max_length=64,
        blank=True,
        db_index=True,
        help_text="Keyed HMAC of the SSN. Match on this, never on the plaintext column.",
    )
    ssn_last4 = models.CharField(
        max_length=4,
        blank=True,
        help_text=(
            "The only part of an SSN any screen displays. Stored separately so masking, "
            "searching and the roster dropdown all work without the plaintext column, "
            "which can then be purged. See the purge_plaintext_ssn command."
        ),
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
            models.Index(fields=["owner", "ssn_fingerprint"], name="mem_owner_ssn_idx"),
            models.Index(fields=["owner", "member_id"], name="mem_owner_memberid_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "client", "member_id"],
                condition=~models.Q(member_id=""),
                name="uniq_member_id_per_owner",
            ),
            check_constraint(
                # A subscriber never points at a subscriber. A dependent normally
                # does, but is allowed not to while its linkage is pending —
                # dependents legitimately arrive before their subscriber in a
                # change-only file, and the previous constraint forced the sync
                # engine to lie about member_type to get such a row saved.
                condition=(models.Q(member_type="SUB") & models.Q(subscriber__isnull=True))
                | (models.Q(member_type="DEP") & models.Q(subscriber__isnull=False))
                | (models.Q(member_type="DEP") & models.Q(subscriber_pending=True)),
                name="dependent_requires_subscriber",
                violation_error_message=(
                    "A subscriber row has no subscriber link; a dependent row needs one "
                    "unless it is explicitly flagged as pending linkage."
                ),
            ),
            check_constraint(
                condition=models.Q(date_of_death__isnull=True)
                | models.Q(date_of_birth__isnull=True)
                | models.Q(date_of_death__gte=models.F("date_of_birth")),
                name="death_not_before_birth",
            ),
        ]

    def save(self, *args, **kwargs):
        self.ssn = normalize_ssn(self.ssn)
        if self.ssn:
            # Derived while the plaintext is still in hand. Once it has been
            # purged these keep their values rather than being blanked.
            self.ssn_fingerprint = ssn_fingerprint(self.ssn)
            self.ssn_last4 = ssn_last4_of(self.ssn)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            fields = set(update_fields)
            # A partial save must still persist the derived columns, or the
            # fingerprint drifts away from the value it is meant to digest.
            if fields & {"ssn"}:
                fields.update({"ssn_fingerprint", "ssn_last4"})
            kwargs["update_fields"] = tuple(fields)
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.ssn:
            SSN_DIGITS(normalize_ssn(self.ssn))

    @property
    def full_name(self):
        return " ".join(part for part in (self.first_name, self.middle_name, self.last_name) if part)

    @property
    def masked_ssn(self):
        """Reads the derived column, so it survives a plaintext purge."""
        last4 = self.ssn_last4 or ssn_last4_of(self.ssn)
        return "XXX-XX-{last4}".format(last4=last4) if last4 else ""

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
            models.Index(fields=["uploaded_file", "member"], name="mds_file_member_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["member", "status_date", "uploaded_file"],
                name="uniq_member_status_per_date",
            )
        ]

    def __str__(self):
        return "{member} present on {date}".format(member=self.member_id, date=self.status_date)


# ---------------------------------------------------------------------------
# Subscriber and Dependant — the separated master tables.
#
# Why these exist alongside Member, rather than replacing it.
#
# Member is the operational person table. It carries the self-referencing
# subscriber link, the eligibility spans, the daily presence rows and the
# identity resolution that stops one person becoming three. All of that is
# load-bearing and none of it is duplicated here.
#
# What Member cannot do is answer "how many subscribers do we hold" or "show me
# the dependants and nothing else" with a table, and it cannot carry a unique
# constraint on SSN, because a subscriber and their spouse legitimately occupy
# the same table and a partial index over a mixed table is a poor way to say
# "one row per person per role". Subscriber and Dependant are that statement,
# maintained by the sync engine as a projection of Member: one row per SSN per
# role per tenant, enforced by the database rather than by convention.
#
# The projection is deliberately one-way. Nothing writes Member from here.
# ---------------------------------------------------------------------------


class RosterPerson(models.Model):
    """Fields common to both master tables. Abstract; creates no table."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    client = models.ForeignKey(
        "users.Client",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="Health plan this record belongs to.",
    )

    ssn = models.CharField(
        max_length=9,
        blank=True,
        validators=[SSN_DIGITS],
        help_text=(
            "Exactly nine digits, no punctuation, leading zeros preserved. Normalised "
            "on save. Unique per tenant when present; blank is allowed because a "
            "sponsor is entitled to send a member with no SSN, and SQLite would treat "
            "several blanks as a collision under a plain unique index."
        ),
    )
    ssn_fingerprint = models.CharField(max_length=64, blank=True, db_index=True)
    ssn_last4 = models.CharField(max_length=4, blank=True)

    member_id = models.CharField(max_length=80, blank=True, db_index=True)
    first_name = models.CharField(max_length=35, blank=True)
    last_name = models.CharField(max_length=60, blank=True)
    dob = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=1, choices=GenderCode.choices, default=GenderCode.UNKNOWN)
    plan = models.CharField(max_length=30, blank=True)
    effective_date = models.DateField(null=True, blank=True)
    termination_date = models.DateField(null=True, blank=True)

    source_file = models.ForeignKey(
        UploadedFile,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text=(
            "Most recent 834 this person was carried in. The full list is in "
            "EnrollmentRecord — this column is the newest one, not the only one."
        ),
    )
    first_source_file = models.ForeignKey(
        UploadedFile,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="The file this person first appeared in. Never overwritten.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.ssn = normalize_ssn(self.ssn)
        if self.ssn:
            self.ssn_fingerprint = ssn_fingerprint(self.ssn)
            self.ssn_last4 = ssn_last4_of(self.ssn)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            fields = set(update_fields)
            if "ssn" in fields:
                fields.update({"ssn_fingerprint", "ssn_last4"})
            kwargs["update_fields"] = tuple(fields)
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.ssn:
            SSN_DIGITS(normalize_ssn(self.ssn))

    @property
    def full_name(self):
        return " ".join(part for part in (self.first_name, self.last_name) if part)

    @property
    def masked_ssn(self):
        last4 = self.ssn_last4 or ssn_last4_of(self.ssn)
        return "XXX-XX-{last4}".format(last4=last4) if last4 else ""


class Subscriber(RosterPerson):
    """
    One subscribing employee or retiree. Never a dependant.

    Issue 2: every upload created a fresh row for a member who was already on
    file, so the same nine digits described three people after three files. The
    unique constraint below makes that a database error rather than a reporting
    surprise, and the sync engine uses update_or_create against it.
    """

    # Named source_member rather than member on purpose: Django gives a
    # ForeignKey named `member` the column attribute `member_id`, which would
    # collide with the sponsor-assigned member_id column the brief specifies.
    source_member = models.OneToOneField(
        Member,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="subscriber_record",
        help_text="Link back to the operational person row this was projected from.",
    )

    class Meta:
        ordering = ("last_name", "first_name")
        indexes = [
            models.Index(fields=["owner", "last_name", "first_name"], name="sub_owner_name_idx"),
            models.Index(fields=["owner", "ssn_fingerprint"], name="sub_owner_ssn_idx"),
        ]
        constraints = [
            # Uniqueness is per tenant rather than global. Sponsor-assigned data
            # is only unique inside one health plan, and a global constraint on
            # SSN would make one plan's roster block another plan from loading
            # the same person — who is genuinely enrolled in both.
            models.UniqueConstraint(
                fields=["owner", "client", "ssn"],
                condition=~models.Q(ssn=""),
                name="uniq_subscriber_ssn_per_tenant",
                violation_error_message="A subscriber with this SSN already exists for this client.",
            ),
            models.UniqueConstraint(
                fields=["owner", "client", "member_id"],
                condition=~models.Q(member_id=""),
                name="uniq_subscriber_member_id_per_tenant",
            ),
        ]

    def __str__(self):
        return "{name} [SUB]".format(name=self.full_name)


class Dependant(RosterPerson):
    """One spouse, child or other dependant, hanging off exactly one Subscriber."""

    subscriber = models.ForeignKey(
        Subscriber,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="dependants",
        help_text=(
            "Null only while the dependant is waiting for its subscriber loop to "
            "arrive. An 834 does not promise the subscriber comes first."
        ),
    )
    relationship = models.CharField(
        max_length=2, choices=RelationshipCode.choices, default=RelationshipCode.CHILD
    )
    source_member = models.OneToOneField(
        Member,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="dependant_record",
    )

    class Meta:
        ordering = ("last_name", "first_name")
        indexes = [
            models.Index(fields=["owner", "last_name", "first_name"], name="dep_owner_name_idx"),
            models.Index(fields=["owner", "ssn_fingerprint"], name="dep_owner_ssn_idx"),
            models.Index(fields=["subscriber"], name="dep_subscriber_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "client", "ssn"],
                condition=~models.Q(ssn=""),
                name="uniq_dependant_ssn_per_tenant",
                violation_error_message="A dependant with this SSN already exists for this client.",
            ),
            models.UniqueConstraint(
                fields=["owner", "client", "member_id"],
                condition=~models.Q(member_id=""),
                name="uniq_dependant_member_id_per_tenant",
            ),
        ]

    def __str__(self):
        return "{name} [DEP]".format(name=self.full_name)


class EnrollmentRecord(models.Model):
    """
    One appearance of one person in one 834, kept forever.

    This is the half of Part 2 that is easy to lose sight of. De-duplicating the
    master record is only correct if the thing it replaces is preserved
    somewhere, otherwise "do not create a second row" quietly becomes "throw the
    second file away". The master tables hold the current truth; this table
    holds every version of it that any file ever asserted, so a member who
    appears in three files has one Subscriber row and three rows here.
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="enrollment_records"
    )
    client = models.ForeignKey(
        "users.Client", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    subscriber = models.ForeignKey(
        Subscriber, null=True, blank=True, on_delete=models.CASCADE, related_name="enrollments"
    )
    dependant = models.ForeignKey(
        Dependant, null=True, blank=True, on_delete=models.CASCADE, related_name="enrollments"
    )

    source_file = models.ForeignKey(
        UploadedFile, on_delete=models.PROTECT, related_name="enrollment_records"
    )
    file_date = models.DateField(null=True, blank=True, db_index=True)

    plan = models.CharField(max_length=30, blank=True)
    insurance_line_code = models.CharField(max_length=3, blank=True)
    effective_date = models.DateField(null=True, blank=True)
    termination_date = models.DateField(null=True, blank=True)
    maintenance_type_code = models.CharField(max_length=3, blank=True)
    relationship = models.CharField(max_length=2, blank=True)
    member_type = models.CharField(max_length=3, choices=MemberType.choices)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-file_date", "-created_at")
        indexes = [
            models.Index(fields=["subscriber", "-file_date"], name="enr_sub_date_idx"),
            models.Index(fields=["dependant", "-file_date"], name="enr_dep_date_idx"),
            models.Index(fields=["source_file"], name="enr_file_idx"),
        ]
        constraints = [
            check_constraint(
                condition=(
                    models.Q(subscriber__isnull=False, dependant__isnull=True)
                    | models.Q(subscriber__isnull=True, dependant__isnull=False)
                ),
                name="enrollment_belongs_to_exactly_one_person",
                violation_error_message=(
                    "An enrollment record belongs to a subscriber or a dependant, not both "
                    "and not neither."
                ),
            ),
            check_constraint(
                condition=models.Q(termination_date__isnull=True)
                | models.Q(effective_date__isnull=True)
                | models.Q(termination_date__gte=models.F("effective_date")),
                name="enr_term_not_before_effective",
            ),
            # One row per person per file per coverage line. A re-upload of the
            # same file updates rather than appending, so history grows with
            # files received and not with times somebody clicked upload.
            models.UniqueConstraint(
                fields=["subscriber", "source_file", "insurance_line_code"],
                condition=models.Q(subscriber__isnull=False),
                name="uniq_sub_enrollment_per_file_line",
            ),
            models.UniqueConstraint(
                fields=["dependant", "source_file", "insurance_line_code"],
                condition=models.Q(dependant__isnull=False),
                name="uniq_dep_enrollment_per_file_line",
            ),
        ]

    @property
    def person(self):
        return self.subscriber or self.dependant

    def __str__(self):
        return "{who} in {file}".format(
            who=self.subscriber_id or self.dependant_id, file=self.source_file_id
        )
