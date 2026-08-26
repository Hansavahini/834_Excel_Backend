"""
Member serialisation.

Two rules hold throughout. Nothing here ever emits a full SSN — the masked form
and the last four digits are what every screen in this application renders, so
sending the digits would put them in browser memory, in the network log and in
any error report for no benefit. And every date is emitted twice: once as ISO,
which is what sorting and filtering need, and once as the MM-DD-YYYY string the
UI displays, so no component has to format a date itself and no two components
can disagree about what "01-02-2025" means.
"""

from django.conf import settings
from rest_framework import serializers

from members.models import (
    Dependant,
    EnrollmentRecord,
    Member,
    MemberDailyStatus,
    MemberEligibilityHistory,
    Subscriber,
)

DISPLAY_DATE_FORMAT = getattr(settings, "DISPLAY_DATE_FORMAT", "%m-%d-%Y")


def display_date(value):
    """MM-DD-YYYY, or an empty string. Never raises on a null column."""
    if not value:
        return ""
    try:
        return value.strftime(DISPLAY_DATE_FORMAT)
    except AttributeError:
        return str(value)


def _file_stamp(uploaded_file):
    """A file reference the card can render without a second request."""
    if uploaded_file is None:
        return None
    return {
        "id": uploaded_file.id,
        "name": uploaded_file.original_filename,
        "file_date": uploaded_file.file_date,
        "file_date_display": display_date(uploaded_file.file_date),
    }


class MemberEligibilityHistorySerializer(serializers.ModelSerializer):
    source_file_name = serializers.CharField(source="source_file.original_filename", read_only=True)
    source_file_id = serializers.IntegerField(read_only=True)
    maintenance_type_display = serializers.CharField(source="get_maintenance_type_code_display", read_only=True)
    effective_date_display = serializers.SerializerMethodField()
    termination_date_display = serializers.SerializerMethodField()

    def get_effective_date_display(self, obj):
        return display_date(obj.effective_date)

    def get_termination_date_display(self, obj):
        return display_date(obj.termination_date)

    class Meta:
        model = MemberEligibilityHistory
        fields = (
            "id",
            "insurance_line_code",
            "plan_code",
            "effective_date",
            "effective_date_display",
            "termination_date",
            "termination_date_display",
            "maintenance_type_code",
            "maintenance_type_display",
            "maintenance_reason_code",
            "source_file_id",
            "source_file_name",
        )


class MemberDailyStatusSerializer(serializers.ModelSerializer):
    file_name = serializers.CharField(source="uploaded_file.original_filename", read_only=True)
    uploaded_file_id = serializers.IntegerField(read_only=True)
    # Kept under its original name because the Member Search screen renders it.
    # It now carries MM-DD-YYYY rather than the ISO string it used to duplicate.
    status_date_display = serializers.SerializerMethodField()

    def get_status_date_display(self, obj):
        return display_date(obj.status_date)

    class Meta:
        model = MemberDailyStatus
        fields = (
            "id",
            "file_name",
            "uploaded_file_id",
            "status_date",
            "status_date_display",
            "change_type",
            "changed_fields",
            "created_at",
        )


class EnrollmentRecordSerializer(serializers.ModelSerializer):
    """
    One appearance of one person in one file.

    This is what Part 13 asks the Member section to show: the same SSN in two
    834s produces one master record and two of these, so the enrollment on
    01-01-2025 is still readable after the file carrying 01-01-2026 arrives.
    """

    file_name = serializers.CharField(source="source_file.original_filename", read_only=True)
    source_file_id = serializers.IntegerField(read_only=True)
    file_date_display = serializers.SerializerMethodField()
    effective_date_display = serializers.SerializerMethodField()
    termination_date_display = serializers.SerializerMethodField()

    def get_file_date_display(self, obj):
        return display_date(obj.file_date)

    def get_effective_date_display(self, obj):
        return display_date(obj.effective_date)

    def get_termination_date_display(self, obj):
        return display_date(obj.termination_date)

    class Meta:
        model = EnrollmentRecord
        fields = (
            "id",
            "source_file_id",
            "file_name",
            "file_date",
            "file_date_display",
            "plan",
            "insurance_line_code",
            "effective_date",
            "effective_date_display",
            "termination_date",
            "termination_date_display",
            "maintenance_type_code",
            "relationship",
            "member_type",
        )


class RosterPersonSerializer(serializers.Serializer):
    """Shared shape for the two master tables. Masked SSN only."""

    id = serializers.IntegerField(read_only=True)
    member_id = serializers.CharField(read_only=True)
    first_name = serializers.CharField(read_only=True)
    last_name = serializers.CharField(read_only=True)
    full_name = serializers.CharField(read_only=True)
    masked_ssn = serializers.CharField(read_only=True)
    ssn_last4 = serializers.CharField(read_only=True)
    dob = serializers.DateField(read_only=True)
    gender = serializers.CharField(read_only=True)
    plan = serializers.CharField(read_only=True)
    effective_date = serializers.DateField(read_only=True)
    termination_date = serializers.DateField(read_only=True)
    dob_display = serializers.SerializerMethodField()
    effective_date_display = serializers.SerializerMethodField()
    termination_date_display = serializers.SerializerMethodField()
    source_file_name = serializers.SerializerMethodField()
    enrollment_count = serializers.SerializerMethodField()
    enrollments = serializers.SerializerMethodField()

    def get_dob_display(self, obj):
        return display_date(obj.dob)

    def get_effective_date_display(self, obj):
        return display_date(obj.effective_date)

    def get_termination_date_display(self, obj):
        return display_date(obj.termination_date)

    def get_source_file_name(self, obj):
        return obj.source_file.original_filename if obj.source_file_id else ""

    def get_enrollment_count(self, obj):
        return obj.enrollments.count()

    def get_enrollments(self, obj):
        return EnrollmentRecordSerializer(
            obj.enrollments.select_related("source_file").order_by(
                "-file_date", "-created_at"
            ),
            many=True,
        ).data


class SubscriberSerializer(RosterPersonSerializer):
    record_type = serializers.SerializerMethodField()
    dependants = serializers.SerializerMethodField()

    def get_record_type(self, obj):
        return "SUBSCRIBER"

    def get_dependants(self, obj):
        return [
            {
                "id": dependant.id,
                "full_name": dependant.full_name,
                "relationship": dependant.relationship,
                "relationship_display": dependant.get_relationship_display(),
                "masked_ssn": dependant.masked_ssn,
                "dob": dependant.dob,
                "dob_display": display_date(dependant.dob),
                "plan": dependant.plan,
            }
            for dependant in obj.dependants.all()
        ]


class DependantSerializer(RosterPersonSerializer):
    record_type = serializers.SerializerMethodField()
    relationship = serializers.CharField(read_only=True)
    relationship_display = serializers.CharField(source="get_relationship_display", read_only=True)
    subscriber = serializers.SerializerMethodField()

    def get_record_type(self, obj):
        return "DEPENDANT"

    def get_subscriber(self, obj):
        if not obj.subscriber_id:
            return None
        return {
            "id": obj.subscriber.id,
            "full_name": obj.subscriber.full_name,
            "member_id": obj.subscriber.member_id,
            "masked_ssn": obj.subscriber.masked_ssn,
        }


class MemberSerializer(serializers.ModelSerializer):
    daily_statuses = MemberDailyStatusSerializer(many=True, read_only=True)
    eligibility_history = MemberEligibilityHistorySerializer(many=True, read_only=True)
    masked_ssn = serializers.CharField(read_only=True)
    full_name = serializers.CharField(read_only=True)
    file_count = serializers.SerializerMethodField()
    source_files = serializers.SerializerMethodField()
    date_of_birth_display = serializers.SerializerMethodField()
    record_type = serializers.SerializerMethodField()
    master_record = serializers.SerializerMethodField()
    enrollment_history = serializers.SerializerMethodField()
    # The self-referencing link, so the browser can group a family without a
    # second request: dependants carry their subscriber's member pk here.
    subscriber_id = serializers.IntegerField(read_only=True)
    subscriber_name = serializers.SerializerMethodField()
    relationship_display = serializers.CharField(
        source="get_relationship_code_display", read_only=True
    )

    display_member_id = serializers.SerializerMethodField()
    member_id_source = serializers.SerializerMethodField()
    recent_changes = serializers.SerializerMethodField()

    def get_subscriber_name(self, obj):
        return obj.subscriber.full_name if obj.subscriber_id and obj.subscriber else ""

    def get_display_member_id(self, obj):
        """
        Something real in the Member ID box, always.

        The card used to render member_id directly and show N/A whenever the
        sponsor identified its members by SSN, which for some sponsors is every
        row of every file. The order below is the same one the converter uses,
        with the portal's own record number as the final fallback so the field
        is never empty: an operator who has to quote a member to somebody can
        always quote something, and member_id_source says which kind of
        identifier they are looking at.

        The SSN is never a candidate here, whatever the sponsor's scheme.
        """
        if obj.member_id:
            return obj.member_id
        if obj.member_type == "SUB" and obj.subscriber_number:
            return obj.subscriber_number
        return "PID-{pk}".format(pk=obj.pk)

    def get_member_id_source(self, obj):
        if obj.member_id:
            return "Sponsor assigned"
        if obj.member_type == "SUB" and obj.subscriber_number:
            return "Subscriber number (REF*0F)"
        return "Portal record number"

    def get_recent_changes(self, obj):
        """
        The last handful of monitored changes for this person, newest first.

        Put on the member card rather than only on the changes screen because
        the question an operator asks when they pull up a member is usually
        "what moved recently", and making them leave the card to find out is a
        worse answer than five rows in the corner of it.
        """
        from members.api.change_serializers import MemberChangeEventSerializer

        rows = obj.change_events.all().order_by("-current_file_date", "-detected_at")[:8]
        return MemberChangeEventSerializer(rows, many=True).data

    date_of_death_display = serializers.SerializerMethodField()
    phone_display = serializers.SerializerMethodField()
    first_seen = serializers.SerializerMethodField()
    last_seen = serializers.SerializerMethodField()

    def get_date_of_birth_display(self, obj):
        return display_date(obj.date_of_birth)

    def get_date_of_death_display(self, obj):
        return display_date(obj.date_of_death)

    def get_phone_display(self, obj):
        """
        Ten stored digits, rendered the way a person reads them. Formatting
        here rather than in the browser so the card, any export and any future
        notification all agree on one spelling.
        """
        digits = (obj.phone or "").strip()
        if len(digits) == 10 and digits.isdigit():
            return "({a}) {b}-{c}".format(a=digits[:3], b=digits[3:6], c=digits[6:])
        return digits

    def get_first_seen(self, obj):
        """The file this person first appeared in, and when."""
        return _file_stamp(obj.first_seen_file)

    def get_last_seen(self, obj):
        return _file_stamp(obj.last_seen_file)

    def get_record_type(self, obj):
        return "SUBSCRIBER" if obj.member_type == "SUB" else "DEPENDANT"

    def get_master_record(self, obj):
        """
        The Subscriber or Dependant row this person occupies.

        Included so a caller can see that two files carrying the same SSN
        resolved to one master record, which is the assertion Part 2 makes and
        the thing a verification screen is there to check.
        """
        record = getattr(obj, "subscriber_record", None) or getattr(
            obj, "dependant_record", None
        )
        if record is None:
            return None
        return {
            "id": record.id,
            "table": "subscriber" if isinstance(record, Subscriber) else "dependant",
            "masked_ssn": record.masked_ssn,
            "member_id": record.member_id,
            "effective_date": record.effective_date,
            "effective_date_display": display_date(record.effective_date),
            "termination_date": record.termination_date,
            "termination_date_display": display_date(record.termination_date),
        }

    def get_enrollment_history(self, obj):
        """Every file this person was carried in, newest first, never collapsed."""
        record = getattr(obj, "subscriber_record", None) or getattr(
            obj, "dependant_record", None
        )
        if record is None:
            return []
        return EnrollmentRecordSerializer(
            record.enrollments.select_related("source_file").order_by(
                "-file_date", "-created_at"
            ),
            many=True,
        ).data

    def get_file_count(self, obj):
        """How many distinct files this member has appeared in."""
        return len({row.uploaded_file_id for row in obj.daily_statuses.all()})

    def get_source_files(self, obj):
        """
        Which files, on what dates, and what changed in each.

        One entry per file rather than per date. Two files carrying the same
        business date used to collapse into one row because MemberDailyStatus
        was unique on (member, status_date), so the earlier file's appearance
        was overwritten and could not be recovered.
        """
        entries = []
        for row in sorted(
            obj.daily_statuses.all(),
            key=lambda r: (r.status_date, r.uploaded_file_id),
            reverse=True,
        ):
            entries.append(
                {
                    "uploaded_file_id": row.uploaded_file_id,
                    "file_name": row.uploaded_file.original_filename,
                    "file_date": row.uploaded_file.file_date,
                    "file_date_display": display_date(row.uploaded_file.file_date),
                    "status_date": row.status_date,
                    "status_date_display": display_date(row.status_date),
                    "change_type": row.change_type,
                    "changed_fields": row.changed_fields,
                }
            )
        return entries

    class Meta:
        model = Member
        fields = (
            "id",
            "member_type",
            "record_type",
            "relationship_code",
            "relationship_display",
            "subscriber_id",
            "subscriber_name",
            "member_id",
            "display_member_id",
            "member_id_source",
            "subscriber_number",
            "group_number",
            "first_name",
            "middle_name",
            "last_name",
            "name_suffix",
            "full_name",
            # The plaintext column is deliberately not serialised. Nothing in
            # the UI renders a full SSN — every screen shows the mask — so
            # shipping the digits to the browser put them in memory, in the
            # network log and in any error report for no benefit at all.
            "masked_ssn",
            "ssn_last4",
            "gender_code",
            "date_of_birth",
            "date_of_birth_display",
            "date_of_death",
            "date_of_death_display",
            "plan_code",
            "class_code",
            "local",
            "benefit_status_code",
            "employment_status_code",
            "student_status_code",
            "address1",
            "address2",
            "city",
            "state",
            "postal_code",
            "phone",
            "phone_display",
            "email",
            "coverage_status",
            "subscriber_pending",
            "first_seen",
            "last_seen",
            "recent_changes",
            "file_count",
            "source_files",
            "master_record",
            "enrollment_history",
            "daily_statuses",
            "eligibility_history",
        )
