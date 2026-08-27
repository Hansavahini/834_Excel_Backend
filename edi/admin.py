from django.contrib import admin
from .models import ProcessingJob

@admin.register(ProcessingJob)
class ProcessingJobAdmin(admin.ModelAdmin):
    list_display = ('id', 'owner', 'kind', 'state', 'progress', 'created_at')
    list_filter = ('kind', 'state')
    search_fields = ('owner__username', 'message')
