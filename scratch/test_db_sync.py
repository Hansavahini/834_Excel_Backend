import os
import django

# Setup Django environment
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth import get_user_model
from members.models import Member, MemberDailyStatus, MemberEligibilityHistory
from edi.api.views import Convert834View
from files.models import UploadedFile
from rest_framework.test import APIRequestFactory

def verify_sync():
    User = get_user_model()
    owner = User.objects.get(username="tpa_analyst")
    
    # 1. Check Baseline
    initial_members = Member.objects.count()
    print(f"--- BASELINE ---")
    print(f"Total Members in Database: {initial_members}")
    
    # 2. Simulate Upload & Convert for the file you have open
    print("\n--- SIMULATING FILE UPLOAD & SYNC ---")
    file_path = "uploads/OOE20250701083954.X12"
    
    # We create a mock request to the Upload API
    factory = APIRequestFactory()
    
    # For testing, we mock the POST request to /api/edi/upload/
    # using the local file we already have in media/uploads/
    from django.core.files.uploadedfile import SimpleUploadedFile
    import os
    from django.conf import settings
    
    full_path = os.path.join(settings.MEDIA_ROOT, file_path)
    import time
    with open(full_path, "rb") as f:
        file_content = f.read()
    
    # Append a timestamp comment to make the hash unique for this test run
    unique_content = file_content + b"~IEA*1*000000001~" + str(time.time()).encode()
    
    upload_file = SimpleUploadedFile(
        name=f"OOE_{int(time.time())}.X12",
        content=unique_content,
        content_type="text/plain"
    )
    
    request = factory.post('/api/edi/upload/', {
        "file": upload_file,
    }, format='multipart')
    request.user = owner
    
    from edi.api.views import EDIUploadView
    view = EDIUploadView.as_view()
    response = view(request)
    
    print(f"API Response Status: {response.status_code}")
    print(f"API Response Data: {response.data}")
    
    # 3. Check Post-Sync Results
    post_members = Member.objects.count()
    added = post_members - initial_members
    print("\n--- POST-SYNC RESULTS ---")
    print(f"Total Members in Database: {post_members}")
    print(f"Members Added: {added}")
    
    # Show Daily Statuses generated today
    statuses = MemberDailyStatus.objects.order_by('-created_at')[:5]
    print("\n--- LATEST AUDIT LOGS (MemberDailyStatus) ---")
    for status in statuses:
        print(f"Member: {status.member.last_name}, {status.member.first_name} | Action: {status.change_type} | Changes: {status.changed_fields}")

if __name__ == "__main__":
    verify_sync()
