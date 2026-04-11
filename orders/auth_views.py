from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from django.contrib.auth import authenticate
from rest_framework.authtoken.models import Token

@api_view(["POST"])
@permission_classes([AllowAny])
def login(request):
    username = request.data.get("username")
    password = request.data.get("password")
    user = authenticate(username=username, password=password)
    if not user:
        return Response({"detail": "Invalid credentials"}, status=400)
    token, _ = Token.objects.get_or_create(user=user)
    roles = list(user.groups.values_list("name", flat=True))
    return Response({"token": token.key, "username": user.username, "roles": roles})

@api_view(["POST"])
def logout(request):
    try:
        request.user.auth_token.delete()
    except Exception:
        pass
    return Response({"ok": True})

# orders/auth_views.py  (or create orders/me_view.py if you prefer)
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me(request):
    u = request.user
    # Return groups as plain names array: ["manager", "staff", ...]
    groups = list(u.groups.values_list("name", flat=True))
    return Response({
        "id": u.id,
        "username": u.username,
        "is_staff": u.is_staff,
        "is_superuser": u.is_superuser,
        "groups": groups,
    })
