from django.contrib.auth import authenticate, login
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny

from django.views.decorators.csrf import ensure_csrf_cookie
from django.utils.decorators import method_decorator

@method_decorator(ensure_csrf_cookie, name='dispatch')
class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        username = request.data.get("username")
        password = request.data.get("password")

        if not username or not password:
            return Response(
                {"error": "Please provide both username and password."},
                status=status.HTTP_400_BAD_REQUEST
            )

        user = authenticate(request, username=username, password=password)

        if user is not None:
            login(request, user)
            return Response(
                {
                    "message": "Login successful",
                    "user_id": user.id,
                    "username": user.username
                },
                status=status.HTTP_200_OK
            )
        else:
            return Response(
                {"error": "Invalid credentials."},
                status=status.HTTP_401_UNAUTHORIZED
            )
