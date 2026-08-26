from django.urls import path

from .api.change_views import (
    MemberChangeBulkAcknowledgeView,
    MemberChangeDetailView,
    MemberChangeListView,
    MemberChangeSummaryView,
)
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
    # The change monitor. Listed before <int:pk>/ would not matter here because
    # "changes" is not an integer, but keeping the literal routes above the
    # catch-all is the habit that stops the next literal route from breaking.
    path("changes/", MemberChangeListView.as_view(), name="member-changes"),
    path("changes/summary/", MemberChangeSummaryView.as_view(), name="member-changes-summary"),
    path("changes/acknowledge/", MemberChangeBulkAcknowledgeView.as_view(), name="member-changes-bulk"),
    path("changes/<int:pk>/", MemberChangeDetailView.as_view(), name="member-change-detail"),
]
