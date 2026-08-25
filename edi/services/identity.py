"""
Match an incoming member loop to a member already on file.

Scoping matters as much as matching here. Every lookup starts from the owner
and, when the deployment has tenancy configured, the client, because an
identifier that is unique inside one health plan is very often reused inside
another — sponsor-assigned member numbers restart at 1 more often than anyone
would like — and a match across that boundary silently merges two people.
"""

from typing import Optional

from members.models import Member


def _scope(owner, client):
    queryset = Member.objects.filter(owner=owner)
    if client is not None:
        queryset = queryset.filter(client=client)
    return queryset


def resolve_member_identity(
    parsed_dict: dict,
    owner,
    current_subscriber: Optional[Member] = None,
    client=None,
) -> Optional[Member]:
    """
    Find an existing member from stable identifiers.

    Order of operations:
      1. Member ID (NM109)
      2. Subscriber + relationship + DOB + gender, for dependents that carry no
         identifier of their own
      3. SSN fingerprint, for subscribers
    """
    scope = _scope(owner, client)

    member_id = (parsed_dict.get("member_id") or "").strip()
    if member_id:
        member = scope.filter(member_id=member_id).first()
        if member:
            return member

    if parsed_dict.get("member_type") == "DEP" and current_subscriber:
        # Some dependents do not get their own unique member_id. Find by
        # subscriber, relationship, DOB and gender.
        dob = parsed_dict.get("date_of_birth")
        gender = parsed_dict.get("gender_code")
        rel_code = parsed_dict.get("relationship_code")

        candidates = scope.filter(
            subscriber=current_subscriber,
            member_type="DEP",
            relationship_code=rel_code,
        )
        if dob:
            candidates = candidates.filter(date_of_birth=dob)
        if gender:
            candidates = candidates.filter(gender_code=gender)

        if candidates.count() == 1:
            return candidates.first()

        # A dependent recorded earlier without its subscriber. Same person,
        # waiting to be joined up rather than duplicated.
        pending = scope.filter(
            member_type="DEP",
            subscriber__isnull=True,
            subscriber_pending=True,
            relationship_code=rel_code,
        )
        if dob:
            pending = pending.filter(date_of_birth=dob)
        if parsed_dict.get("last_name"):
            pending = pending.filter(last_name=parsed_dict["last_name"])
        if pending.count() == 1:
            return pending.first()

    ssn = parsed_dict.get("ssn")
    if ssn:
        from members.models import ssn_fingerprint

        fingerprint = ssn_fingerprint(ssn)
        if fingerprint:
            member = scope.filter(
                ssn_fingerprint=fingerprint,
                member_type=parsed_dict.get("member_type") or "SUB",
            ).first()
            if member:
                return member

    return None
