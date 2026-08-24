import os, sys, django
sys.path.insert(0, os.path.abspath('.'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from members.models import Member
from django.db.models import Count

# Ignore empty member_ids since dependents often don't have them
base_qs = Member.objects.exclude(member_id='')

# User's query: ignores owner, will find cross-tenant duplicates
all_duplicates = list(
    base_qs
    .values('member_id')
    .annotate(total=Count('id'))
    .filter(total__gt=1)[:5] # limit output
)
total_all_dupes = base_qs.values('member_id').annotate(total=Count('id')).filter(total__gt=1).count()

# Correct query: grouping by owner and member_id
owner_duplicates = list(
    base_qs
    .values('owner__username', 'member_id')
    .annotate(total=Count('id'))
    .filter(total__gt=1)[:5]
)
total_owner_dupes = base_qs.values('owner__username', 'member_id').annotate(total=Count('id')).filter(total__gt=1).count()

print("=== Cross-Account Duplicates (Expected due to multi-tenancy) ===")
print(f"Total duplicate member_ids across ALL users: {total_all_dupes}")
for d in all_duplicates:
    print(f"Member ID: {d['member_id']} (Found {d['total']} times)")

print("\n=== Single-Account Duplicates (The Real Test) ===")
print(f"Total duplicate member_ids within the SAME user account: {total_owner_dupes}")
if not owner_duplicates:
    print("SUCCESS! No duplicates found when properly checking by user.")
else:
    for d in owner_duplicates:
        print(f"User {d['owner__username']} has duplicate Member ID: {d['member_id']} (Found {d['total']} times)")
