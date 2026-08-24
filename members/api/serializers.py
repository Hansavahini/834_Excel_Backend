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
            "masked_ssn",
            "ssn",
            "gender_code",
            "date_of_birth",
            "plan_code",
            "class_code",
            "coverage_status",
            "daily_statuses",
            "eligibility_history"
        )
