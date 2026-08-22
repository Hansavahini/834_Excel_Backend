"""
Request validation for the EDI endpoints.

The convert endpoint previously read request.data["file_path"], ["headers"] and
["mappings"] directly, so a missing key raised KeyError and DRF turned it into a
500 with a stack trace. A malformed request is a client error and should say
which field is wrong.
"""

from django.conf import settings
from rest_framework import serializers

ALLOWED_EXTENSIONS = (".834", ".x12", ".edi", ".txt", ".dat")


class EDIFileUploadSerializer(serializers.Serializer):
    file = serializers.FileField()

    def validate_file(self, value):
        name = value.name.lower()
        if not any(name.endswith(ext) for ext in ALLOWED_EXTENSIONS):
            raise serializers.ValidationError(
                "Invalid file type. Accepted extensions: {exts}.".format(
                    exts=", ".join(ALLOWED_EXTENSIONS)
                )
            )

        limit = getattr(settings, "MAX_834_UPLOAD_BYTES", 200 * 1024 * 1024)
        if value.size and value.size > limit:
            raise serializers.ValidationError(
                "File is {size:.0f} MB; the limit is {limit:.0f} MB.".format(
                    size=value.size / 1e6, limit=limit / 1e6
                )
            )
        if not value.size:
            raise serializers.ValidationError("File is empty.")

        # Extension proves nothing. Sniff the first bytes for an ISA header so a
        # renamed PDF is rejected at the door rather than three stages later.
        head = value.read(3)
        value.seek(0)
        if head.lstrip(b"\xef\xbb\xbf")[:3].upper() != b"ISA":
            raise serializers.ValidationError(
                "File does not start with an ISA segment, so it is not an X12 interchange."
            )
        return value


class MappingSerializer(serializers.Serializer):
    """One Excel column rule."""

    excel_column = serializers.CharField(max_length=64)
    segment = serializers.CharField(max_length=3)
    element = serializers.CharField(max_length=6)
    qualifier_element = serializers.CharField(max_length=6, required=False, allow_blank=True, default="")
    qualifier_value = serializers.CharField(max_length=12, required=False, allow_blank=True, default="")
    component_index = serializers.IntegerField(required=False, allow_null=True, min_value=1, default=None)
    occurrence = serializers.IntegerField(required=False, min_value=1, default=1)
    column_order = serializers.IntegerField(required=False, allow_null=True, min_value=1, default=None)
    applies_to = serializers.ChoiceField(choices=("BOTH", "SUB", "DEP"), required=False, default="BOTH")
    transform = serializers.CharField(max_length=16, required=False, default="NONE")
    default_value = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    is_required = serializers.BooleanField(required=False, default=False)
    template_name = serializers.CharField(max_length=120, required=False, default="Default")

    def validate(self, attrs):
        element = attrs["element"].upper()
        segment = attrs["segment"].upper()
        if not element.startswith(segment):
            raise serializers.ValidationError(
                {"element": "Element {e} does not belong to segment {s}.".format(e=element, s=segment)}
            )
        # The model enforces this too; catching it here gives a field-level error
        # instead of an IntegrityError from the check constraint.
        if bool(attrs.get("qualifier_element")) != bool(attrs.get("qualifier_value")):
            raise serializers.ValidationError(
                "Give both a qualifier element and a qualifier value, or neither."
            )
        attrs["segment"] = segment
        attrs["element"] = element
        return attrs


class ConvertRequestSerializer(serializers.Serializer):
    """
    Either reference a saved template, or pass headers and mappings inline.
    """

    file_path = serializers.CharField(max_length=500, required=False, allow_blank=True)
    uploaded_file_id = serializers.IntegerField(required=False, allow_null=True)
    mapping_template_id = serializers.IntegerField(required=False, allow_null=True)
    headers = serializers.ListField(child=serializers.CharField(max_length=64), required=False)
    mappings = MappingSerializer(many=True, required=False)

    def validate(self, attrs):
        if not attrs.get("file_path") and not attrs.get("uploaded_file_id"):
            raise serializers.ValidationError(
                "Supply either uploaded_file_id or the file_path returned by the upload endpoint."
            )
        if attrs.get("mappings") and not attrs.get("headers"):
            # Headers default to the mapping columns in order; without this the
            # workbook silently comes out blank when the two lists disagree.
            attrs["headers"] = [rule["excel_column"] for rule in attrs["mappings"]]
        if attrs.get("headers") and attrs.get("mappings"):
            columns = {rule["excel_column"] for rule in attrs["mappings"]}
            orphans = [h for h in attrs["headers"] if h not in columns]
            if orphans:
                raise serializers.ValidationError(
                    {"headers": "No mapping rule produces these columns: {cols}.".format(
                        cols=", ".join(orphans))}
                )
        return attrs
