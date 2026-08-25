"""
Mapping persistence, with versions that stay true after the fact.

The store used to be a module-level Python list, which meant a rule saved
through one gunicorn worker was invisible to the next request, every rule
vanished on restart, and none of it reached the MappingTemplate /
MappingDetail / SegmentElement tables that were already designed, migrated and
wired into the admin.

The second problem, fixed here, was versioning. The template carried a version
number that nothing ever incremented: edits landed on the same row, so a
ConversionHistory that recorded "mapping version 1" was pointing at whatever
version 1 had been edited into since. An auditor asking "what rules produced
this workbook" got an answer that was true only by accident.

So a version is immutable once used. save_mapping writes to the newest unlocked
version of a template; if that version has been locked by a completed
conversion, it is cloned to version n+1 and the edit lands there. Nothing that
history refers to is ever rewritten.
"""

from __future__ import annotations

from typing import List, Optional

from django.db import transaction
from django.utils import timezone

from mapping.models import MappingDetail, MappingTemplate, SegmentElement

from .element_codes import normalize_element, normalize_segment


def _segment_element(segment: str, element: str) -> SegmentElement:
    """
    Look the pair up in the dictionary, creating an inactive placeholder when a
    sponsor sends something the seeded TR3 list does not have. Better a flagged
    unknown element than a rejected mapping the user cannot save.
    """
    segment = normalize_segment(segment)
    element = normalize_element(element, segment)
    obj = SegmentElement.objects.filter(
        segment_name=segment, element_code=element
    ).first()
    if obj:
        return obj
    return SegmentElement.objects.create(
        segment_name=segment,
        element_code=element,
        description="Added from a mapping rule; confirm against the implementation guide.",
        is_active=False,
    )


def _clone_details(source: MappingTemplate, target: MappingTemplate) -> None:
    """Copy every rule onto the new version, so an edit is a delta not a reset."""
    for detail in source.details.all():
        MappingDetail.objects.create(
            mapping_template=target,
            excel_column=detail.excel_column,
            column_order=detail.column_order,
            segment_element=detail.segment_element,
            qualifier_element=detail.qualifier_element,
            qualifier_value=detail.qualifier_value,
            component_index=detail.component_index,
            occurrence=detail.occurrence,
            applies_to=detail.applies_to,
            transform=detail.transform,
            default_value=detail.default_value,
            is_required=detail.is_required,
        )


def writable_template(owner, template_name: str = "Default", client=None) -> MappingTemplate:
    """
    The version an edit may be written to.

    Returns the newest version of the named template when it is still unlocked,
    otherwise a fresh clone at the next version number. Never mutates a version
    a completed conversion has referred to.
    """
    latest = (
        MappingTemplate.objects.filter(
            owner=owner, client=client, mapping_name=template_name
        )
        .order_by("-version")
        .first()
    )

    if latest is None:
        return MappingTemplate.objects.create(
            owner=owner,
            client=client,
            mapping_name=template_name,
            version=1,
            description="Created from the mapping API.",
        )

    if not latest.is_locked:
        return latest

    successor = MappingTemplate.objects.create(
        owner=owner,
        client=client,
        mapping_name=template_name,
        version=latest.version + 1,
        description=(
            "Version {n} of {name}, cloned because version {prev} had already been "
            "used by a completed conversion."
        ).format(n=latest.version + 1, name=template_name, prev=latest.version),
        is_default=False,
        is_active=True,
    )
    _clone_details(latest, successor)

    # The default flag follows the newest editable version, so the next
    # conversion picks up the edit without the caller naming a template id.
    if latest.is_default:
        MappingTemplate.objects.filter(pk=latest.pk).update(is_default=False)
        MappingTemplate.objects.filter(pk=successor.pk).update(is_default=True)
        successor.refresh_from_db()

    return successor


def lock_template(template: Optional[MappingTemplate]) -> None:
    """Freeze a version the moment a conversion has finished using it."""
    if template is None or template.locked_at is not None:
        return
    MappingTemplate.objects.filter(pk=template.pk, locked_at__isnull=True).update(
        locked_at=timezone.now()
    )


@transaction.atomic
def save_mapping(rule: dict, owner, template_name: str = "Default", client=None) -> dict:
    """Persist one column rule against a user's writable template version."""
    template = writable_template(owner, template_name, client=client)

    order = rule.get("column_order")
    if not order:
        existing = template.details.filter(excel_column=rule["excel_column"]).first()
        if existing:
            order = existing.column_order
        else:
            highest = (
                template.details.order_by("-column_order")
                .values_list("column_order", flat=True)
                .first()
            )
            order = (highest or 0) + 1

    segment = normalize_segment(rule["segment"])

    detail, _created = MappingDetail.objects.update_or_create(
        mapping_template=template,
        excel_column=rule["excel_column"],
        defaults={
            "column_order": order,
            "segment_element": _segment_element(segment, rule["element"]),
            "qualifier_element": normalize_element(
                rule.get("qualifier_element", "") or "", segment
            ),
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


def get_template(owner, template_id: Optional[int] = None, client=None) -> Optional[MappingTemplate]:
    """Named template, else the user's default, else their most recent."""
    queryset = MappingTemplate.objects.filter(owner=owner, is_active=True)
    if client is not None:
        queryset = queryset.filter(client=client)
    if template_id:
        return queryset.filter(pk=template_id).first()
    return (
        queryset.filter(is_default=True).order_by("-version").first()
        or queryset.order_by("-updated_at", "-version").first()
    )


def get_mappings(owner, template_id: Optional[int] = None, client=None) -> List[MappingDetail]:
    template = get_template(owner, template_id, client=client)
    if not template:
        return []
    return list(
        template.details.select_related("segment_element").order_by("column_order")
    )


def headers_for(details: List[MappingDetail]) -> List[str]:
    """Column headers in template order, so the workbook matches the mapping."""
    return [detail.excel_column for detail in details]
