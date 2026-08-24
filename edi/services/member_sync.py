from typing import Optional, Tuple
from django.db import transaction
from django.utils import timezone
from members.models import Member, MemberEligibilityHistory, MemberDailyStatus, CoverageStatus
from .identity import resolve_member_identity

def get_changed_fields(member: Member, parsed_dict: dict) -> dict:
    """Compare fields and return a dict of what changed."""
    changed = {}
    fields_to_check = [
        "first_name", "last_name", "middle_name", "gender_code", "date_of_birth", 
        "address1", "address2", "city", "state", "postal_code", "phone", "email",
        "plan_code", "class_code"
    ]
    for field in fields_to_check:
        old_val = getattr(member, field)
        new_val = parsed_dict.get(field)
        if new_val is not None and old_val != new_val:
            changed[field] = [old_val, new_val]
    return changed

def sync_member_demographics(member: Member, parsed_dict: dict, changed_fields: dict):
    """Update member with new fields."""
    for field, (old_val, new_val) in changed_fields.items():
        setattr(member, field, new_val)
    # Never overwrite a valid member_id with blank
    if parsed_dict.get("member_id") and not member.member_id:
        member.member_id = parsed_dict["member_id"]
    member.save()

def sync_eligibility(member: Member, parsed_dict: dict, source_file) -> str:
    """Handle the eligibility span coverage and return the action string."""
    eff_date = parsed_dict.get("effective_date")
    term_date = parsed_dict.get("termination_date")
    mtc = parsed_dict.get("maintenance_type_code")
    plan = parsed_dict.get("plan_code")
    
    # 024 means cancellation/termination
    is_terminating = mtc == "024"
    
    # Find active span
    active_span = MemberEligibilityHistory.objects.filter(
        member=member, 
        termination_date__isnull=True
    ).first()

    action = "UNCHANGED"

    if is_terminating:
        if active_span:
            active_span.termination_date = term_date or timezone.now().date()
            active_span.maintenance_type_code = mtc
            active_span.save()
            action = "TERMINATED"
            member.coverage_status = CoverageStatus.TERMINATED
            member.save()
    else:
        # Addition, Change, or Reinstatement
        if not active_span:
            # Create new coverage
            MemberEligibilityHistory.objects.create(
                member=member,
                effective_date=eff_date or timezone.now().date(),
                plan_code=plan,
                maintenance_type_code=mtc,
                source_file=source_file
            )
            action = "REINSTATED" if mtc == "025" else "ADDED"
            member.coverage_status = CoverageStatus.ACTIVE
            member.save()
        else:
            # Coverage exists, but did plan change?
            if plan and active_span.plan_code != plan:
                active_span.termination_date = eff_date or timezone.now().date()
                active_span.save()
                
                MemberEligibilityHistory.objects.create(
                    member=member,
                    effective_date=eff_date or timezone.now().date(),
                    plan_code=plan,
                    maintenance_type_code=mtc,
                    source_file=source_file
                )
                action = "CHANGED"

    return action

@transaction.atomic
def sync_member_loop(parsed_dict: dict, owner, source_file, status_date, current_subscriber: Optional[Member] = None) -> Tuple[Member, str, dict]:
    """
    Main sync function called for every member loop.
    Returns (Member instance, change_type_string, changed_fields_dict).
    """
    member = resolve_member_identity(parsed_dict, owner, current_subscriber)
    
    change_type = MemberDailyStatus.ChangeType.UNCHANGED
    changed_fields = {}

    if not member:
        # ADD / New Member
        member = Member.objects.create(
            owner=owner,
            member_type=parsed_dict.get("member_type", "SUB"),
            subscriber=current_subscriber,
            relationship_code=parsed_dict.get("relationship_code", "18"),
            first_name=parsed_dict.get("first_name", ""),
            last_name=parsed_dict.get("last_name", ""),
            date_of_birth=parsed_dict.get("date_of_birth"),
            gender_code=parsed_dict.get("gender_code", "U"),
            member_id=parsed_dict.get("member_id", ""),
            ssn=parsed_dict.get("ssn", ""),
            plan_code=parsed_dict.get("plan_code", ""),
            first_seen_file=source_file,
            last_seen_file=source_file
        )
        change_type = MemberDailyStatus.ChangeType.ADDED
    else:
        # Existing Member, update if needed
        changed_fields = get_changed_fields(member, parsed_dict)
        if changed_fields:
            sync_member_demographics(member, parsed_dict, changed_fields)
            change_type = MemberDailyStatus.ChangeType.CHANGED
            
        member.last_seen_file = source_file
        member.save(update_fields=['last_seen_file'])

    # Sync eligibility and coverage
    eligibility_action = sync_eligibility(member, parsed_dict, source_file)
    
    # If the coverage status changed and the demographics didn't, mark as changed
    if eligibility_action in ("TERMINATED", "REINSTATED") and change_type == MemberDailyStatus.ChangeType.UNCHANGED:
        change_type = getattr(MemberDailyStatus.ChangeType, eligibility_action)
    elif eligibility_action == "CHANGED":
        change_type = MemberDailyStatus.ChangeType.CHANGED

    # Write Daily Status
    MemberDailyStatus.objects.update_or_create(
        member=member,
        status_date=status_date,
        defaults={
            "uploaded_file": source_file,
            "change_type": change_type,
            "changed_fields": changed_fields
        }
    )

    return member, change_type, changed_fields
