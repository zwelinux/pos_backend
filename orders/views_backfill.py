# orders/views_backfill.py
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from .permissions import InGroups
from rest_framework.response import Response
from rest_framework import status
from .serializers import BackfillOrderInSer
from .models import Expense, CashSession
from django.utils import timezone

@api_view(["POST"])
@permission_classes([IsAuthenticated, InGroups("Manager")])
def backfill_order(request):
    ser = BackfillOrderInSer(data=request.data, context={"request": request})
    ser.is_valid(raise_exception=True)
    order = ser.save()
    return Response({"id": order.id, "number": order.number, "total": str(order.total)}, status=201)
