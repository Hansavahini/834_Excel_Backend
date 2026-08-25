from django.urls import path

from .api.roster_views import FileDatesView, MemberRosterView, SSNOptionsView
from .api.views import (
    DependantListView,
    MemberDetailView,
    MemberSearchView,
    SubscriberListView,
)

urlpatterns = [
    path("search/", MemberSearchView.as_view(), name="member-search"),
    # Part 3: the separated master tables, readable on their own.
    path("subscribers/", SubscriberListView.as_view(), name="subscriber-list"),
    path("dependants/", DependantListView.as_view(), name="dependant-list"),
    path("<int:pk>/", MemberDetailView.as_view(), name="member-detail"),
    # Admin-only Info section.
    path("file-dates/", FileDatesView.as_view(), name="member-file-dates"),
    path("ssn-options/", SSNOptionsView.as_view(), name="member-ssn-options"),
    path("roster/", MemberRosterView.as_view(), name="member-roster"),
]
