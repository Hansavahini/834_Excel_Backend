from django.contrib import admin

from .models import CustodialParent, Member, MemberDailyStatus, MemberEligibilityHistory


class DependentInline(admin.TabularInline):
    model = Member
    fk_name = "subscriber"
    extra = 0
    fields = ("first_name", "last_name", "relationship_code", "gender_code", "date_of_birth", "coverage_status")
    readonly_fields = fields
    can_delete = False
    show_change_link = True
    verbose_name_plural = "Dependents"

    def has_add_permission(self, request, obj=None):
        return False


class EligibilityInline(admin.TabularInline):
    model = MemberEligibilityHistory
    extra = 0
    fields = ("insurance_line_code", "effective_date", "termination_date", "effective_date_as_per_law", "termination_date_as_per_law", "maintenance_type_code", "source_file")
    readonly_fields = fields
    can_delete = False
    ordering = ("-effective_date",)

    def has_add_permission(self, request, obj=None):
        return False


class CustodialParentInline(admin.StackedInline):
    model = CustodialParent
    extra = 0


@admin.register(Member)
class MemberAdmin(admin.ModelAdmin):
    list_display = ("last_name", "first_name", "owner", "member_type", "member_id", "masked_ssn_display", "coverage_status", "plan_code")
    list_filter = ("owner", "member_type", "coverage_status", "gender_code", "plan_code", "class_code")
    # SSN is deliberately absent from search_fields: a search term reaches logs,
    # browser history and the referer header.
    search_fields = ("last_name", "first_name", "member_id", "subscriber_number", "email")
    autocomplete_fields = ("owner", "subscriber", "first_seen_file", "last_seen_file")
    readonly_fields = ("ssn_fingerprint", "created_at", "updated_at", "masked_ssn_display")
    exclude = ("ssn",)
    inlines = (CustodialParentInline, DependentInline, EligibilityInline)

    @admin.display(description="SSN")
    def masked_ssn_display(self, obj):
        return obj.masked_ssn or "not on file"

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MemberEligibilityHistory)
class MemberEligibilityHistoryAdmin(admin.ModelAdmin):
    list_display = ("member", "insurance_line_code", "effective_date", "termination_date", "effective_date_as_per_law", "termination_date_as_per_law", "maintenance_type_code", "source_file")
    list_filter = ("insurance_line_code", "maintenance_type_code", "effective_date")
    search_fields = ("member__last_name", "member__first_name", "member__member_id")
    autocomplete_fields = ("member", "source_file")
    date_hierarchy = "effective_date"

    def has_delete_permission(self, request, obj=None):
        # Rule 6: historical eligibility is never removed.
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(MemberDailyStatus)
class MemberDailyStatusAdmin(admin.ModelAdmin):
    list_display = ("status_date", "member", "change_type", "uploaded_file")
    list_filter = ("change_type", "status_date")
    search_fields = ("member__last_name", "member__member_id")
    autocomplete_fields = ("member", "uploaded_file")
    date_hierarchy = "status_date"

    def has_change_permission(self, request, obj=None):
        return False


# ---------------------------------------------------------------------------
# The separated master tables.
#
# Registered read-only. These are a projection maintained by the sync engine,
# not a place to correct data by hand: an edit here would be silently reverted
# by the next file carrying that member, and in the meantime the master row and
# the Member row it came from would disagree. Fix the source or fix the mapping.
# ---------------------------------------------------------------------------

from members.models import Dependant, EnrollmentRecord, Subscriber


class ReadOnlyProjection(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Subscriber)
class SubscriberAdmin(ReadOnlyProjection):
    # masked_ssn, never the ssn column. A changelist renders hundreds of rows.
    list_display = ("last_name", "first_name", "masked_ssn", "member_id", "plan",
                    "effective_date", "termination_date")
    list_filter = ("client", "plan")
    search_fields = ("last_name", "first_name", "member_id", "ssn_last4")
    readonly_fields = ("masked_ssn",)
    exclude = ("ssn", "ssn_fingerprint")


@admin.register(Dependant)
class DependantAdmin(ReadOnlyProjection):
    list_display = ("last_name", "first_name", "masked_ssn", "relationship",
                    "subscriber", "plan", "effective_date")
    list_filter = ("client", "relationship")
    search_fields = ("last_name", "first_name", "member_id", "ssn_last4")
    readonly_fields = ("masked_ssn",)
    exclude = ("ssn", "ssn_fingerprint")


@admin.register(EnrollmentRecord)
class EnrollmentRecordAdmin(ReadOnlyProjection):
    list_display = ("source_file", "file_date", "member_type", "plan",
                    "effective_date", "termination_date")
    list_filter = ("member_type", "file_date")
