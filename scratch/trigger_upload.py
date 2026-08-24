import requests
import json
import os

url = "http://localhost:8000/api/edi/upload/"
file_path = "media/uploads/testing.X12"

# 1. Login to get session
login_url = "http://localhost:8000/api/users/login/"
session = requests.Session()
login_res = session.post(login_url, json={"username": "tpa_analyst", "password": "password"}) # Adjust if needed

print(f"Login Status: {login_res.status_code}")

# If we don't have a real password for the user, we can just use the auth token or skip if auth is off
# Since we just added the login endpoint earlier, we can use it.
# Actually, since it requires auth, and we don't know the password...
