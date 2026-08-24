from django.urls import path
from .api.views import MemberSearchView

urlpatterns = [
    path("search/", MemberSearchView.as_view(), name="member-search"),
]
