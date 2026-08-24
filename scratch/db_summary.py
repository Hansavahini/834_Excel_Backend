import os
import sys
import django

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

def run():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()

    from members.models import Member, MemberDailyStatus, MemberEligibilityHistory
    from files.models import UploadedFile
    
    print(f"--- DATABASE SUMMARY ---")
    print(f"Total Uploaded Files: {UploadedFile.objects.count()}")
    print(f"Total Unique Members: {Member.objects.count()}")
    print(f"Total Eligibility Spans: {MemberEligibilityHistory.objects.count()}")
    print(f"Total Daily Audit Logs: {MemberDailyStatus.objects.count()}")
    
    print(f"\n--- RECENT ACTIVITY (Last 5 Updates) ---")
    recent_logs = MemberDailyStatus.objects.order_by('-created_at')[:5]
    for log in recent_logs:
        print(f"[{log.created_at.strftime('%Y-%m-%d %H:%M:%S')}] {log.member.first_name} {log.member.last_name}: {log.change_type} | Changes: {log.changed_fields}")

if __name__ == "__main__":
    run()
