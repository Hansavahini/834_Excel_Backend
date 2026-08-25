from rest_framework import serializers
from members.models import Member, MemberDailyStatus, MemberEligibilityHistory

class MemberEligibilityHistorySerializer(serializers.ModelSerializer):
    source_file_name = serializers.CharField(source="source_file.original_filename", read_only=True)
    maintenance_type_display = serializers.CharField(source="get_maintenance_type_code_display", read_only=True)

    class Meta:
        model = MemberEligibilityHistory
        fields = (
            "id",
            "insurance_line_code",
            "plan_code",
            "effective_date",
            "termination_date",
            "maintenance_type_code",
            "maintenance_type_display",
            "maintenance_reason_code",
            "source_file_name",
        )

class MemberDailyStatusSerializer(serializers.ModelSerializer):
    file_name = serializers.CharField(source="uploaded_file.original_filename", read_only=True)
    status_date_display = serializers.DateField(source="status_date", read_only=True)

    class Meta:
        model = MemberDailyStatus
        fields = (
            "id",
            "file_name",
            "status_date",
            "status_date_display",
            "change_type",
            "changed_fields",
            "created_at",
        )

class MemberSerializer(serializers.ModelSerializer):
    daily_statuses = MemberDailyStatusSerializer(many=True, read_only=True)
    eligibility_history = MemberEligibilityHistorySerializer(many=True, read_only=True)
    masked_ssn = serializers.CharField(read_only=True)
    full_name = serializers.CharField(read_only=True)
    file_count = serializers.SerializerMethodField()
    source_files = serializers.SerializerMethodField()

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
                    "status_date": row.status_date,
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
            "relationship_code",
            "member_id",
            "subscriber_number",
            "group_number",
            "first_name",
            "last_name",
            "full_name",
            # The plaintext column is deliberately not serialised. Nothing in
            # the UI renders a full SSN — every screen shows the mask — so
            # shipping the digits to the browser put them in memory, in the
            # network log and in any error report for no benefit at all.
            "masked_ssn",
            "ssn_last4",
            "gender_code",
            "date_of_birth",
            "plan_code",
            "class_code",
            "coverage_status",
            "subscriber_pending",
            "file_count",
            "source_files",
            "daily_statuses",
            "eligibility_history"
        )
