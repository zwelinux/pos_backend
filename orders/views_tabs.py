# orders/views_tabs.py
from datetime import datetime, time as t
from decimal import Decimal

from django.db import models
from django.db.models import Prefetch
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Order, OrderItem  # uses your existing models

def _parse_date(s: str):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None

def _day_bounds(d):
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(d, t(0, 0, 0)), tz)
    end = timezone.make_aware(datetime.combine(d, t(23, 59, 59, 999000)), tz)
    return start, end

def _serialize_order(o: Order):
    return {
        "id": o.id,
        "number": o.number,
        "table": {"id": o.table_id, "name": getattr(o.table, "name", None)} if o.table_id else None,
        "customer_name": o.customer_name,
        "credit_remark": o.credit_remark,
        "subtotal": str(o.subtotal),
        "tax": str(o.tax),
        "total": str(o.total),
        "status": o.status,
        "payment_method": o.payment_method,
        "tab_opened_at": o.tab_opened_at,
        "paid_at": o.paid_at,
        "items": [
            {
                "id": it.id,
                "qty": it.qty,
                "product_name": it.product.name,
                "variant_name": it.variant.name if it.variant_id else None,
                "line_total": str(it.line_total),
                "notes": it.notes,
                "modifiers": [
                    {
                        "option_id": m.option_id,
                        "option_name": m.option.name,
                        "include": m.include,
                        "price_delta": str(m.price_delta),
                    } for m in it.modifiers.all()
                ],
            } for it in o.items.all()
        ],
    }

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def tabs_paid(request):
    """
    List **paid tabs** (orders that were on tab and are now settled).
    Query params:
      - q=... (order number / customer / table)
      - from=YYYY-MM-DD
      - to=YYYY-MM-DD
    """
    q = (request.GET.get("q") or "").strip()
    dfrom = _parse_date(request.GET.get("from") or "")
    dto   = _parse_date(request.GET.get("to") or "")

    qs = (
        Order.objects
        .select_related("table")
        .prefetch_related(
            Prefetch("items", queryset=OrderItem.objects.prefetch_related("modifiers", "product", "variant"))
        )
        .filter(
            status="paid",
            tab_opened_at__isnull=False,  # it was a tab at some point
            paid_at__isnull=False,        # and is settled
        )
        .order_by("-paid_at")
    )

    if dfrom:
        start, _ = _day_bounds(dfrom)
        qs = qs.filter(paid_at__gte=start)
    if dto:
        _, end = _day_bounds(dto)
        qs = qs.filter(paid_at__lte=end)

    if q:
        qs = qs.filter(
            models.Q(number__icontains=q) |
            models.Q(customer_name__icontains=q) |
            models.Q(table__name__icontains=q)
        )

    data = [_serialize_order(o) for o in qs[:300]]
    return Response(data, status=200)
