"""
Serialisation for the change monitor.

Two things this layer is careful about.

It never emits an SSN, in any form, including inside old_value or new_value.
The SSN is not a watched field so it cannot get there through the normal path,
but a serializer is the wrong place to rely on that: _safe_value below strips
anything that looks like nine consecutive digits regardless of which field it
came from. The last four are sent as their own column because that is what an
operator uses to confirm they are looking at the right person.

And it sends every value twice where a value has two useful forms — the raw one
for sorting and filtering, and the display one for rendering. A date change from
"1960-01-15" to "1961-01-15" is unreadable in a table; the same change shown as
"01-15-1960 to 01-15-1961" is the whole point of the screen.
"""

from __future__ import annotations

import re

from django.conf import settings
from rest_framework import serializers

from edi.services.change_monitor import label_for
from members.models import MemberChangeEvent

DISPLAY_DATE_FORMAT = getattr(settings, "DISPLAY_DATE_FORMAT", "%m-%d-%Y")

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NINE_DIGITS = re.compile(r"\b\d{9}\b")


def _display(value):
    """MM-DD-YYYY, or an empty string. Never raises on a null column."""
    if not value:
        return ""
    try:
        return value.strftime(DISPLAY_DATE_FORMAT)
    except AttributeError:
        return str(value)


def _safe_value(text: str) -> str:
    """
    One stored value, rendered for a screen.

    An ISO date becomes MM-DD-YYYY so the table reads the way every other date
    in the portal does. Nine consecutive digits are masked whatever field they
    came from — a belt-and-braces guard, since no watched field should ever
    carry an SSN, and the cost of being wrong about that is a plaintext SSN in
    a browser's memory and in every error report the page generates.
    """
    text = (text or "").strip()
    if not text:
        return ""
    if _ISO_DATE.match(text):
        year, month, day = text.split("-")
        return "{m}-{d}-{y}".format(m=month, d=day, y=year)
    return _NINE_DIGITS.sub(lambda match: "XXXXX" + match.group(0)[-4:], text)


class MemberChangeEventSerializer(serializers.ModelSerializer):
    field_label = serializers.SerializerMethodField()
    old_display = serializers.SerializerMethodField()
    new_display = serializers.SerializerMethodField()
    previous_file_name = serializers.SerializerMethodField()
    current_file_name = serializers.SerializerMethodField()
    previous_file_date_display = serializers.SerializerMethodField()
    current_file_date_display = serializers.SerializerMethodField()
    acknowledged_by_name = serializers.SerializerMethodField()
    is_open = serializers.BooleanField(read_only=True)
    # The card and the change table both want to link back to the person.
    member_pk = serializers.IntegerField(source="member_id", read_only=True)
    member_id = serializers.CharField(source="sponsor_member_id", read_only=True)

    def get_field_label(self, obj):
        return label_for(obj.field_name)

    def get_old_display(self, obj):
        return _safe_value(obj.old_value)

    def get_new_display(self, obj):
        return _safe_value(obj.new_value)

    def get_previous_file_name(self, obj):
        return obj.previous_file.original_filename if obj.previous_file_id else ""

    def get_current_file_name(self, obj):
        return obj.current_file.original_filename if obj.current_file_id else ""

    def get_previous_file_date_display(self, obj):
        return _display(obj.previous_file_date)

    def get_current_file_date_display(self, obj):
        return _display(obj.current_file_date)

    def get_acknowledged_by_name(self, obj):
        if not obj.acknowledged_by_id:
            return ""
        user = obj.acknowledged_by
        return user.get_full_name() or user.get_username()

    class Meta:
        model = MemberChangeEvent
        fields = (
            "id",
            "member_pk",
            "member_id",
            "member_name",
            "member_type",
            "ssn_last4",
            "field_name",
            "field_label",
            "old_value",
            "new_value",
            "old_display",
            "new_display",
            "category",
            "severity",
            "previous_file",
            "previous_file_name",
            "previous_file_date",
            "previous_file_date_display",
            "current_file",
            "current_file_name",
            "current_file_date",
            "current_file_date_display",
            "acknowledged_at",
            "acknowledged_by_name",
            "note",
            "is_open",
            "detected_at",
        )
        read_only_fields = fields
