from django.urls import path

from .api.roster_views import FileDatesView, MemberRosterView, SSNOptionsView
from .api.views import MemberDetailView, MemberSearchView

urlpatterns = [
    path("search/", MemberSearchView.as_view(), name="member-search"),
    path("<int:pk>/", MemberDetailView.as_view(), name="member-detail"),
    # Admin-only Info section.
    path("file-dates/", FileDatesView.as_view(), name="member-file-dates"),
    path("ssn-options/", SSNOptionsView.as_view(), name="member-ssn-options"),
    path("roster/", MemberRosterView.as_view(), name="member-roster"),
]
