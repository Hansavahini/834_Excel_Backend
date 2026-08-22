from django.contrib import admin

from .models import MappingDetail, MappingTemplate, SegmentElement


class MappingDetailInline(admin.TabularInline):
    model = MappingDetail
    extra = 1
    ordering = ("column_order",)
    autocomplete_fields = ("segment_element",)
    fields = (
        "column_order", "excel_column", "segment_element", "qualifier_element",
        "qualifier_value", "component_index", "applies_to", "transform", "is_required",
    )


@admin.register(MappingTemplate)
class MappingTemplateAdmin(admin.ModelAdmin):
    list_display = ("mapping_name", "version", "owner", "is_default", "is_active", "updated_at")
    list_filter = ("is_default", "is_active")
    search_fields = ("mapping_name", "description")
    autocomplete_fields = ("owner",)
    inlines = (MappingDetailInline,)


@admin.register(SegmentElement)
class SegmentElementAdmin(admin.ModelAdmin):
    list_display = ("element_code", "segment_name", "description", "loop_id", "data_type", "is_active")
    list_filter = ("segment_name", "is_active", "loop_id")
    search_fields = ("segment_name", "element_code", "description")
    ordering = ("segment_name", "element_code")


@admin.register(MappingDetail)
class MappingDetailAdmin(admin.ModelAdmin):
    list_display = ("mapping_template", "column_order", "excel_column", "segment_element", "qualifier_value", "transform")
    list_filter = ("transform", "applies_to", "is_required")
    search_fields = ("excel_column",)
    autocomplete_fields = ("mapping_template", "segment_element")
