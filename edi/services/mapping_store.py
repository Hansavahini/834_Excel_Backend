"""
Mapping persistence.

The previous implementation was a module-level Python list:

    mapping_rules = []
    def save_mapping(mapping): mapping_rules.append(mapping); return mapping

That list lived in one worker process. Under any real deployment gunicorn runs
several workers, so a rule saved through one of them was invisible to the next
request, every rule vanished on restart, no rule belonged to a user, and none of
it reached the MappingTemplate / MappingDetail / SegmentElement tables that were
already designed, migrated and wired into the admin. The mapping app had a
database layer that nothing used.

These functions read and write those tables instead.
"""

from __future__ import annotations

from typing import List, Optional

from django.db import transaction

from mapping.models import MappingDetail, MappingTemplate, SegmentElement


def _segment_element(segment: str, element: str) -> SegmentElement:
    """
    Look the pair up in the dictionary, creating an inactive placeholder when a
    sponsor sends something the seeded TR3 list does not have. Better a flagged
    unknown element than a rejected mapping the user cannot save.
    """
    obj = SegmentElement.objects.filter(
        segment_name=segment.upper(), element_code=element.upper()
    ).first()
    if obj:
        return obj
    return SegmentElement.objects.create(
        segment_name=segment.upper(),
        element_code=element.upper(),
        description="Added from a mapping rule; confirm against the implementation guide.",
        is_active=False,
    )


@transaction.atomic
def save_mapping(rule: dict, owner, template_name: str = "Default") -> dict:
    """Persist one column rule against a user's template."""
    template, _ = MappingTemplate.objects.get_or_create(
        owner=owner,
        mapping_name=template_name,
        version=1,
        defaults={"description": "Created from the mapping API."},
    )

    order = rule.get("column_order")
    if not order:
        highest = template.details.order_by("-column_order").values_list("column_order", flat=True).first()
        order = (highest or 0) + 1

    detail, _created = MappingDetail.objects.update_or_create(
        mapping_template=template,
        excel_column=rule["excel_column"],
        defaults={
            "column_order": order,
            "segment_element": _segment_element(rule["segment"], rule["element"]),
            "qualifier_element": rule.get("qualifier_element", "") or "",
            "qualifier_value": rule.get("qualifier_value", "") or "",
            "component_index": rule.get("component_index"),
            "occurrence": rule.get("occurrence") or 1,
            "applies_to": rule.get("applies_to") or "BOTH",
            "transform": rule.get("transform") or "NONE",
            "default_value": rule.get("default_value", "") or "",
            "is_required": bool(rule.get("is_required")),
        },
    )

    return {
        "id": detail.id,
        "template_id": template.id,
        "template": template.mapping_name,
        "version": template.version,
        "excel_column": detail.excel_column,
        "column_order": detail.column_order,
        "segment": detail.segment,
        "element": detail.element,
        "qualifier_element": detail.qualifier_element,
        "qualifier_value": detail.qualifier_value,
        "occurrence": detail.occurrence,
        "applies_to": detail.applies_to,
        "transform": detail.transform,
    }


def get_template(owner, template_id: Optional[int] = None) -> Optional[MappingTemplate]:
    """Named template, else the user's default, else their most recent."""
    queryset = MappingTemplate.objects.filter(owner=owner, is_active=True)
    if template_id:
        return queryset.filter(pk=template_id).first()
    return queryset.filter(is_default=True).first() or queryset.order_by("-updated_at").first()


def get_mappings(owner, template_id: Optional[int] = None) -> List[MappingDetail]:
    template = get_template(owner, template_id)
    if not template:
        return []
    return list(
        template.details.select_related("segment_element").order_by("column_order")
    )


def headers_for(details: List[MappingDetail]) -> List[str]:
    """Column headers in template order, so the workbook matches the mapping."""
    return [detail.excel_column for detail in details]
