from django.contrib import admin

# Register your models here.

from django.contrib import admin as _admin

from .models import Client, ClientMembership


@_admin.register(Client)
class ClientAdmin(_admin.ModelAdmin):
    list_display = ("name", "code", "is_active", "created_at")
    search_fields = ("name", "code")
    list_filter = ("is_active",)


@_admin.register(ClientMembership)
class ClientMembershipAdmin(_admin.ModelAdmin):
    list_display = ("user", "client", "is_default")
    list_filter = ("client", "is_default")
    search_fields = ("user__username", "client__name")
