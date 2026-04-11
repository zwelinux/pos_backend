# orders/permissions.py
from rest_framework.permissions import BasePermission

def InGroups(*names):
    class _P(BasePermission):
        def has_permission(self, request, view):   # <- include view
            user = getattr(request, "user", None)
            if not (user and user.is_authenticated):
                return False
            return user.groups.filter(name__in=names).exists()
    return _P
from rest_framework.permissions import BasePermission

def InGroups(*names):
    class _P(BasePermission):
        def has_permission(self, request, view):   # must accept (request, view)
            u = getattr(request, "user", None)
            return bool(u and u.is_authenticated and u.groups.filter(name__in=names).exists())
    return _P

# orders/permissions.py
from rest_framework.permissions import BasePermission

def InGroups(*names):
    want = {str(n).strip().lower() for n in names}

    class _P(BasePermission):
        def has_permission(self, request, view):  # must accept (request, view)
            u = getattr(request, "user", None)
            if not (u and u.is_authenticated):
                return False
            if u.is_superuser or u.is_staff:
                return True
            return u.groups.filter(name__in=names).exists() or \
                   u.groups.filter(name__in=[n.upper() for n in want]).exists() or \
                   u.groups.filter(name__in=[n.title() for n in want]).exists()
    return _P
