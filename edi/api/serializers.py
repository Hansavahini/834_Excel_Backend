"""
Request validation for the EDI endpoints.

The convert endpoint previously read request.data["file_path"], ["headers"] and
["mappings"] directly, so a missing key raised KeyError and DRF turned it into a
500 with a stack trace. A malformed request is a client error and should say
which field is wrong.
"""

from django.conf import settings
from rest_framework import serializers

from edi.services.element_codes import (
    element_position,
    normalize_element,
    normalize_segment,
)

ALLOWED_EXTENSIONS = (".834", ".x12", ".edi", ".txt", ".dat")


class EDIFileUploadSerializer(serializers.Serializer):
    file = serializers.FileField()

    def validate_file(self, value):
        # Extension proves nothing. Sniff the first bytes for an ISA header so a
        # renamed PDF is rejected before reaching the parser.

        head = value.read(64)
        value.seek(0)

        UTF8_BOM = b"\xef\xbb\xbf"

        if head.startswith(UTF8_BOM):
            head = head[len(UTF8_BOM):]

        if head[:3].upper() != b"ISA":
            raise serializers.ValidationError(
                "File does not start with an ISA segment, so it is not an X12 interchange."
            )

        return value


class MappingSerializer(serializers.Serializer):
    """One Excel column rule."""

    excel_column = serializers.CharField(max_length=64)
    segment = serializers.CharField(max_length=3)
    element = serializers.CharField(max_length=6)

    qualifier_element = serializers.CharField(
        max_length=6,
        required=False,
        allow_blank=True,
        default=""
    )

    qualifier_value = serializers.CharField(
        max_length=12,
        required=False,
        allow_blank=True,
        default=""
    )

    component_index = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=1,
        default=None
    )

    occurrence = serializers.IntegerField(
        required=False,
        min_value=1,
        default=1
    )

    column_order = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=1,
        default=None
    )

    applies_to = serializers.ChoiceField(
        choices=("BOTH", "SUB", "DEP"),
        required=False,
        default="BOTH"
    )

    transform = serializers.CharField(
        max_length=16,
        required=False,
        default="NONE"
    )

    default_value = serializers.CharField(
        max_length=120,
        required=False,
        allow_blank=True,
        default=""
    )

    is_required = serializers.BooleanField(
        required=False,
        default=False
    )

    template_name = serializers.CharField(
        max_length=120,
        required=False,
        default="Default"
    )

    def validate(self, attrs):
        # The UI displays NM1-03; the resolver needs NM103. Normalise on the way
        # in so exactly one spelling is ever stored, and so a hyphenated code
        # from an older client build is accepted rather than silently producing
        # a blank column.
        segment = normalize_segment(attrs["segment"])
        element = normalize_element(attrs["element"], segment)

        if element_position(element, segment) is None:
            raise serializers.ValidationError(
                {
                    "element": (
                        "Element {e} does not name a numbered position in segment {s}. "
                        "Expected something like {s}03."
                    ).format(
                        e=attrs["element"],
                        s=segment
                    )
                }
            )

        if attrs.get("qualifier_element"):
            qualifier = normalize_element(attrs["qualifier_element"], segment)
            if element_position(qualifier, segment) is None:
                raise serializers.ValidationError(
                    {
                        "qualifier_element": (
                            "Qualifier {q} does not name a numbered position in segment {s}."
                        ).format(q=attrs["qualifier_element"], s=segment)
                    }
                )
            attrs["qualifier_element"] = qualifier

        # The model enforces this too; catching it here gives a field-level error
        # instead of an IntegrityError from the check constraint.
        if bool(attrs.get("qualifier_element")) != bool(
            attrs.get("qualifier_value")
        ):
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

    file_path = serializers.CharField(
        max_length=500,
        required=False,
        allow_blank=True
    )

    uploaded_file_id = serializers.IntegerField(
        required=False,
        allow_null=True
    )

    mapping_template_id = serializers.IntegerField(
        required=False,
        allow_null=True
    )

    headers = serializers.ListField(
        child=serializers.CharField(max_length=64),
        required=False
    )

    # Alias. The workbook schema and the mapping rules are two different things,
    # and "columns" is the clearer name for the first of them, so the API takes
    # either and normalises onto headers.
    columns = serializers.ListField(
        child=serializers.CharField(max_length=64),
        required=False
    )

    mappings = MappingSerializer(
        many=True,
        required=False
    )

    def validate(self, attrs):
        if not attrs.get("file_path") and not attrs.get("uploaded_file_id"):
            raise serializers.ValidationError(
                "Supply either uploaded_file_id or the file_path returned by the upload endpoint."
            )

        if attrs.get("columns") and not attrs.get("headers"):
            attrs["headers"] = attrs["columns"]
        attrs.pop("columns", None)

        if attrs.get("mappings") and not attrs.get("headers"):
            # Headers default to the mapping columns in order.
            attrs["headers"] = [
                rule["excel_column"]
                for rule in attrs["mappings"]
            ]

        # A header with no mapping rule behind it is not an error. The UI has a
        # fixed column layout and some of those columns (LOCAL, CLASS) are filled
        # in downstream by hand, so the workbook must still carry the column with
        # empty cells. Rejecting the request here is what made those columns
        # vanish from the output entirely.
        if attrs.get("headers"):
            seen = set()
            deduped = []
            for header in attrs["headers"]:
                if header in seen:
                    continue
                seen.add(header)
                deduped.append(header)
            attrs["headers"] = deduped

        if attrs.get("mappings"):
            # A rule whose column is not in the schema would write a value
            # nobody can see. Append it rather than dropping the rule silently.
            headers = attrs.get("headers") or []
            known = set(headers)
            for rule in attrs["mappings"]:
                if rule["excel_column"] not in known:
                    known.add(rule["excel_column"])
                    headers.append(rule["excel_column"])
            attrs["headers"] = headers

        return attrs