import os
import sys
import json
import django

# Add backend directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

def run():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()

    from django.conf import settings
    settings.ALLOWED_HOSTS.append('testserver')

    from django.test import Client
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user, _ = User.objects.get_or_create(username="tpa_analyst")

    client = Client()
    client.force_login(user)

    payload = {
        "file_path": "uploads/OOE20250701083954.X12",
        "headers": [
            "MEMBER ID", "LAST NAME", "FIRST NAME", "DOB", 
            "GENDER", "EFFECTIVE DATE", "PLAN"
        ],
        "mappings": [
            {"excel_column": "MEMBER ID", "segment": "NM1", "element": "NM109", "occurrence": 1},
            {"excel_column": "LAST NAME", "segment": "NM1", "element": "NM103", "occurrence": 1},
            {"excel_column": "FIRST NAME", "segment": "NM1", "element": "NM104", "occurrence": 1},
            {"excel_column": "DOB", "segment": "DMG", "element": "DMG02", "occurrence": 1},
            {"excel_column": "GENDER", "segment": "DMG", "element": "DMG03", "occurrence": 1},
            {"excel_column": "EFFECTIVE DATE", "segment": "DTP", "element": "DTP03", "occurrence": 1},
            {"excel_column": "PLAN", "segment": "HD", "element": "HD03", "occurrence": 1}
        ]
    }

    print("Sending POST request to /api/edi/convert/...")
    response = client.post(
        "/api/edi/convert/", 
        data=json.dumps(payload),
        content_type="application/json"
    )

    print(f"Status Code: {response.status_code}")
    try:
        print("Response JSON:")
        print(json.dumps(response.json(), indent=2))
    except Exception as e:
        print("Response Content:")
        print(response.content.decode('utf-8'))

if __name__ == "__main__":
    run()
