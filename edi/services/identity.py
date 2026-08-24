from typing import Optional
from members.models import Member

def resolve_member_identity(parsed_dict: dict, owner, current_subscriber: Optional[Member] = None) -> Optional[Member]:
    """
    Finds an existing member based on stable identifiers.
    
    Order of operations:
    1. Member ID (NM109)
    2. Subscriber Number (REF*0F) + Relationship Code + DOB + Gender (for dependents)
    """
    # 1. Try by Member ID if present and unique
    member_id = parsed_dict.get("member_id", "").strip()
    if member_id:
        member = Member.objects.filter(owner=owner, member_id=member_id).first()
        if member:
            return member

    # 2. If it's a dependent, try matching by subscriber + relationship + demographic
    if parsed_dict.get("member_type") == "DEP" and current_subscriber:
        # Some dependents do not get their own unique member_id.
        # Find by subscriber, relationship, DOB, and Gender.
        dob = parsed_dict.get("date_of_birth")
        gender = parsed_dict.get("gender_code")
        rel_code = parsed_dict.get("relationship_code")
        
        candidates = Member.objects.filter(
            owner=owner,
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
            
    # 3. Last resort for subscribers without member_id, try SSN if present
    ssn = parsed_dict.get("ssn")
    if ssn:
        from members.models import ssn_fingerprint
        fingerprint = ssn_fingerprint(ssn)
        member = Member.objects.filter(owner=owner, ssn_fingerprint=fingerprint, member_type="SUB").first()
        if member:
            return member

    return None
