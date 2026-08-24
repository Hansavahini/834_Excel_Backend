import os
import sys
import django

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

def run():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()

    from django.conf import settings
    if 'testserver' not in settings.ALLOWED_HOSTS:
        settings.ALLOWED_HOSTS.append('testserver')

    from django.test import Client
    from django.contrib.auth import get_user_model
    from members.models import Member, MemberDailyStatus

    User = get_user_model()
    user, _ = User.objects.get_or_create(username="tpa_analyst")

    client = Client()
    client.force_login(user)

    # 1. Check baseline members
    initial_count = Member.objects.count()
    print(f"--- BASELINE ---")
    print(f"Members before upload: {initial_count}")

    # 2. Upload the file
    print("\n--- UPLOADING testing.X12 ---")
    
    file_path = os.path.join(settings.MEDIA_ROOT, "uploads", "testing.X12")
    
    if not os.path.exists(file_path):
        print(f"ERROR: Could not find file at {file_path}")
        return

    with open(file_path, 'rb') as f:
        response = client.post('/api/edi/upload/', {'file': f}, format='multipart')

    print(f"API Status: {response.status_code}")
    print(f"API Response: {response.json()}")

    # 3. Check Database after upload
    final_count = Member.objects.count()
    added = final_count - initial_count
    print(f"\n--- POST-UPLOAD RESULTS ---")
    print(f"Members after upload: {final_count}")
    print(f"Members successfully ADDED by Sync Engine: {added}")

    if added > 0:
        print("\n--- NEW MEMBER DAILY STATUSES ---")
        statuses = MemberDailyStatus.objects.order_by('-created_at')[:5]
        for s in statuses:
             print(f"[{s.change_type}] {s.member.first_name} {s.member.last_name}")

if __name__ == "__main__":
    from django.conf import settings
    run()
