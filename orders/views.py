from datetime import datetime, timedelta, time
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum, F
from django.utils import timezone

from rest_framework import mixins, status, viewsets
from rest_framework.decorators import api_view, permission_classes, action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from .realtime import notify_kds


from catalog.models import Product, ProductVariant, ModifierOption, ProductModifierGroup
from .models import Order, OrderItem, KitchenTicket, Table, OrderItemModifier
from .permissions import InGroups
from .serializers import (
    OrderCreateSer,
    OrderOutSer,
    KitchenTicketOutSer,
    TableSer,
    OrderAddItemsSer,
    OrderSettleSer,
    _clean_station,   # use the shared helper from serializers.py
    _option_allowed_for_product, 
    OrderOpenTabSer,
)
from collections import Counter
import re

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from orders.models import KitchenTicket

from collections import Counter
import re
from django.db.models import Count
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from catalog.models import Product
from .models import KitchenTicket
from .serializers import _clean_station  

from rest_framework.pagination import PageNumberPagination
from django.db.models import Q, Count
from .serializers import OrderListSer, OrderOutSer, TableAdminInSer

from datetime import date as date_cls, datetime, time, timedelta
from django.utils import timezone

from django.db.models import Sum, Count, F, Q, Value as V
from django.db.models.functions import Coalesce
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import *
from decimal import Decimal
from django.db.models import Sum, Count, Value as V, DecimalField, IntegerField
from django.db.models.functions import Coalesce, Cast

# top of your file
from datetime import date as date_cls, datetime, time, timedelta, timezone as dt_timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:
    try:
        from backports.zoneinfo import ZoneInfo
    except ImportError:
        import pytz as ZoneInfo

from django.utils import timezone
from decimal import Decimal
from django.db.models import Value as V, DecimalField

try:
    STORE_TZ = ZoneInfo("Asia/Bangkok")  # your local shop timezone
except TypeError:
    import pytz
    STORE_TZ = pytz.timezone("Asia/Bangkok")

DEC0 = V(Decimal("0.00"), output_field=DecimalField(max_digits=12, decimal_places=2))


def local_day_window_utc(day_str: str | None):
    """
    Interpret `day_str` (YYYY-MM-DD) in STORE_TZ and return (utc_start, utc_end).
    If None/invalid, use today's date in STORE_TZ.
    """
    try:
        day = date_cls.fromisoformat(day_str) if day_str else timezone.now().astimezone(STORE_TZ).date()
    except Exception:
        day = timezone.now().astimezone(STORE_TZ).date()

    local_start = datetime.combine(day, time.min, tzinfo=STORE_TZ)
    local_end   = local_start + timedelta(days=1)

    # ✅ Use Python's datetime.timezone.utc instead of django.utils.timezone.utc
    return local_start.astimezone(dt_timezone.utc), local_end.astimezone(dt_timezone.utc)


_ST_RE = re.compile(r"\s+")

def _pretty(code: str) -> str:
    # "TESTING_STATION" -> "Testing Station"
    return re.sub(r"[_.-]+", " ", code).title()



from django.utils import timezone
from .serializers import OrderVoidSer
from django.db.models import Sum, F, Count
from rest_framework.exceptions import ValidationError

from .models import Order, OrderItem, KitchenTicket, Table, OrderItemModifier, OrderComp
from .serializers import (
    OrderCreateSer, OrderOutSer, KitchenTicketOutSer, TableSer,
    OrderAddItemsSer, OrderSettleSer, _clean_station, _option_allowed_for_product,
    OrderOpenTabSer,
    # NEW:
    OrderCompInSer, OrderCompOutSer,
)

class SmallPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 200



from django.shortcuts import get_object_or_404
from .serializers import OrderItemOutSer  # you already have this


def _modifier_show_title(product_id, group_id) -> bool:
    if not product_id or not group_id:
        return True
    show_title = (
        ProductModifierGroup.objects
        .filter(product_id=product_id, group_id=group_id)
        .values_list("show_title", flat=True)
        .first()
    )
    return True if show_title is None else bool(show_title)


class OrderViewSet(mixins.CreateModelMixin,
                   mixins.RetrieveModelMixin,
                   mixins.ListModelMixin,
                   viewsets.GenericViewSet):
    pagination_class = SmallPagination
    queryset = (
        Order.objects.all()
        .order_by("-id")
        .select_related("table", "paid_by", "tab_opened_by", "tab_closed_by")
        .prefetch_related(
            "items__product",
            "items__variant",
            "items__modifiers__option",
            "tickets",
            "comps",
             "items__comps",
        )
    )

    def _mods_key_from_request(self, mods):
        pairs = []
        for m in mods or []:
            try:
                oid = int(m.get("option_id"))
            except Exception:
                continue
            include = bool(m.get("include", True))
            qty = int(m.get("qty", 1))
            pairs.append((oid, include, qty))
        pairs.sort()
        return tuple(pairs)

    def _mods_key_for_item(self, item):
        pairs = [(im.option_id, bool(im.include), int(im.qty)) for im in item.modifiers.all()]
        pairs.sort()
        return tuple(pairs)

    # ---------- small helper for safe broadcasts ----------
    def _group_send(self, station: str, msg: dict):
        """Fire a Channels group_send safely (used inside transaction.on_commit)."""
        try:
            layer = get_channel_layer()
            async_to_sync(layer.group_send)(f"kitchen.{station}", msg)
        except Exception:
            pass

    def get_serializer_class(self):
        if self.action == "list":
            return OrderListSer
        return OrderOutSer

    def get_permissions(self):
        if self.action == "retrieve":
            return [AllowAny()]
        if self.action in ("create", "attach_table", "add_items", "settle"):
            return [IsAuthenticated(), InGroups("Cashier", "Manager")()]
        if self.action == "void":
            return [IsAuthenticated(), InGroups("Cashier", "Manager")()]
        if self.action in ("list_comps", "create_comp", "void_comp"):
                return [IsAuthenticated(), InGroups("Cashier", "Manager")()]
        if self.action == "list":
            return [IsAuthenticated(), InGroups("Manager")()]
        return [IsAuthenticated()]

    def create(self, request, *args, **kwargs):
        ser = OrderCreateSer(data=request.data)
        ser.is_valid(raise_exception=True)
        order = ser.save()

        # NEW: if paid immediately, record who took the payment
        try:
            pay_now = bool(request.data.get("pay_now", True))
        except Exception:
            pay_now = True
        if pay_now and getattr(request, "user", None) and not getattr(order, "paid_by_id", None):
            order.paid_by = request.user
            order.save(update_fields=["paid_by"])

        return Response(
            OrderOutSer(order, context={"request": request}).data,
            status=status.HTTP_201_CREATED
        )

    # def create(self, request, *args, **kwargs):
    #     ser = OrderCreateSer(data=request.data)
    #     ser.is_valid(raise_exception=True)
    #     order = ser.save()
    #     return Response(
    #         OrderOutSer(order, context={"request": request}).data,
    #         status=status.HTTP_201_CREATED
    #     )

    def retrieve(self, request, pk=None, *args, **kwargs):
        order = self.get_object()
        return Response(OrderOutSer(order, context={"request": request}).data)

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        ser = OrderVoidSer(data=request.data)
        ser.is_valid(raise_exception=True)
        reason = ser.validated_data.get("reason", "")
        free_table_param = ser.validated_data.get("free_table", None)
        do_free = True if free_table_param is None else bool(free_table_param)

        with transaction.atomic():
            order = Order.objects.select_for_update().get(pk=pk)

            if order.paid_at:
                return Response({"detail": "Cannot void a paid order."}, status=400)
            if order.status == "void":
                return Response({"detail": "Order already void."}, status=400)

            order.status = "void"
            order.void_reason = reason
            order.voided_by = getattr(request, "user", None)
            order.voided_at = timezone.now()
            order.save(update_fields=["status", "void_reason", "voided_by", "voided_at"])

            # free table (except Takeaway)
            tid = order.table_id
            if do_free and tid:
                is_takeaway = Table.objects.filter(pk=tid, name__iexact="takeaway").exists()
                if not is_takeaway:
                    Table.objects.select_for_update().filter(pk=tid).update(status="free")

            # close open tickets (DB)
            now = timezone.now()
            KitchenTicket.objects.filter(
                order=order, status__in=["queued", "in_progress"]
            ).update(status="done", done_at=now)

            # build cancel payloads while rows still exist, then send AFTER COMMIT
            table_name = order.table.name if order.table else ""
            payloads = []
            for kt in KitchenTicket.objects.filter(order=order):
                payloads.append((
                    kt.station,
                    {
                        "type": "kitchen.cancel",
                        "data": {
                            "id": kt.id,
                            "order_number": order.number,
                            "station": kt.station,
                            "status": "cancelled",
                            "table_name": table_name,
                        },
                    },
                ))
            transaction.on_commit(lambda: [self._group_send(st, msg) for st, msg in payloads])

        return Response(OrderOutSer(order, context={"request": request}).data)

    # -----------------------------
    # Attach Table
    # -----------------------------
    @action(detail=True, methods=["patch"])
    def attach_table(self, request, pk=None):
        order = self.get_object()

        new_tid = request.data.get("table_id", None)
        if new_tid == "":
            new_tid = None

        with transaction.atomic():
            # 1) Free previous table if needed
            prev = order.table
            if prev and prev.name.lower() != "takeaway" and prev.status != "free":
                Table.objects.select_for_update().filter(pk=prev.pk).update(status="free")

            # 2) Assign new table (or detach)
            if new_tid is None:
                order.table = None
            else:
                try:
                    t = Table.objects.select_for_update().get(pk=new_tid)
                except Table.DoesNotExist:
                    return Response({"detail": "Invalid table_id"}, status=400)

                if t.status == "closed":
                    return Response({"detail": "Table is closed"}, status=400)

                order.table = t
                if t.name.lower() != "takeaway" and t.status != "occupied":
                    t.status = "occupied"
                    t.save(update_fields=["status"])

            order.save(update_fields=["table"])

        # 3) Broadcast table badge update
        table_name = order.table.name if order.table else ""
        try:
            layer = get_channel_layer()
            for kt in order.tickets.all():
                if not kt.station:
                    continue
                async_to_sync(layer.group_send)(
                    f"kitchen.{kt.station}",
                    {
                        "type": "kitchen.update",
                        "data": {"id": kt.id, "table_name": table_name, "status": kt.status},
                    },
                )
        except Exception as e:
            print("Broadcast failed:", e)

        return Response({"ok": True, "table": TableSer(order.table).data if order.table else None})

    def _broadcast_table_change(self, tickets, table_name):
        try:
            layer = get_channel_layer()
            for kt in tickets:
                async_to_sync(layer.group_send)(
                    f"kitchen.{kt.station}",
                    {
                        "type": "kitchen.update",
                        "data": {"id": kt.id, "table_name": table_name, "status": kt.status},
                    },
                )
        except Exception:
            pass


    # -----------------------------
    @action(detail=True, methods=["post"])
    def add_items(self, request, pk=None):
        order = self.get_object()
        if order.paid_at:
            return Response({"detail": "Order already settled. Create a new order."}, status=400)

        ser = OrderAddItemsSer(data=request.data)
        ser.is_valid(raise_exception=True)
        tax_rate = ser.validated_data["tax_rate"]
        items = ser.validated_data["items"]

        broadcasts = []
        with transaction.atomic():
            for it in items:
                # --- product (must be ACTIVE) ---
                try:
                    p = Product.objects.get(pk=it["product_id"], is_active=True)
                    p.refresh_from_db()
                except Product.DoesNotExist:
                    return Response({"detail": "Product not found or inactive."}, status=400)

                # --- variant resolution (simplified & robust) ---
                v = None
                base = Decimal(p.base_price)
                v_id = it.get("variant_id")
                v_name = (it.get("variant_name") or "").strip()

                if v_id:
                    # ✅ Try direct ID lookup first (safe even if inactive)
                    v = ProductVariant.objects.filter(id=v_id, product=p).first()
                elif v_name:
                    # ✅ Match by name if provided
                    v_norm = v_name.strip().casefold()
                    for cand in ProductVariant.objects.filter(product=p):
                        if cand.name and cand.name.strip().casefold() == v_norm:
                            v = cand
                            break

                # ✅ Apply variant delta if variant found
                if v:
                    base += Decimal(v.price_delta or 0)

                notes = (it.get("notes", "") or "")
                qty = int(it["qty"])

                # --- materialize modifiers and compute price ---
                total_delta = Decimal("0.00")
                mods_in = it.get("modifiers", []) or []
                mods_materialized, ticket_mods = [], []

                for m in mods_in:
                    opt = ModifierOption.objects.get(pk=m["option_id"])
                    if not _option_allowed_for_product(p, opt):
                        raise ValidationError({
                            "items": f"Modifier option #{opt.id} ({opt.name}) "
                                    f"is not allowed for product #{p.id} ({p.name})."
                        })
                    include = bool(m.get("include", True))
                    qty_mod = int(m.get("qty", 1))
                    delta = Decimal(opt.price_delta) if include else Decimal("0.00")
                    total_delta += delta * qty_mod
                    mods_materialized.append((opt, include, delta, qty_mod))
                    ticket_mods.append({
                        "option_id": opt.id,
                        "option_name": opt.name,
                        "group_name": getattr(opt.group, "name", "Options"),
                        "show_title": _modifier_show_title(p.id, opt.group_id),
                        "include": include,
                        "price_delta": str(delta),
                        "qty": qty_mod,
                    })

                # ✅ Final unit computation
                unit = base + total_delta
                incoming_key = self._mods_key_from_request(mods_in)

                # --- merge identical lines if possible ---
                candidates = (
                    OrderItem.objects.select_for_update()
                    .filter(order=order, product=p, variant=v, notes=notes, unit_price=unit)
                    .prefetch_related("modifiers", "tickets")
                    .order_by("-id")
                )
                merge_target = next((c for c in candidates if self._mods_key_for_item(c) == incoming_key), None)
                station = _clean_station(p.kitchen_station)

                if merge_target:
                    merge_target.qty = int(merge_target.qty) + qty
                    merge_target.recompute(save=False)
                    merge_target.save(update_fields=["qty", "line_total"])
                    for _ in range(qty):
                        kt = KitchenTicket.objects.create(
                            order=order,
                            item=merge_target,
                            station=station,
                            status="in_progress",
                            started_at=timezone.now(),
                        )

                        broadcasts.append({
                            "station": station,
                            "type": "kitchen.ticket",
                            "data": {
                                "id": kt.id,
                                "order_number": order.number,
                                "station": station,
                                "status": kt.status,
                                "product_name": merge_target.product_name_snapshot,
                                "variant_name": merge_target.variant_name_snapshot,
                                "qty": 1,  # 🔒 ALWAYS 1
                                "modifiers": ticket_mods,
                                "table_name": order.table.name if order.table else "",
                                "started_at": kt.started_at.isoformat(),
                            },
                        })
                    continue

                # --- create new line with immutable name snapshots ---
                oi = OrderItem.objects.create(
                    order=order,
                    product=p,
                    variant=v,
                    qty=qty,
                    unit_price=unit,
                    line_total=unit * qty,
                    notes=notes,
                    product_name_snapshot=p.name,
                    variant_name_snapshot=(v.name if v else ""),
                    base_price_snapshot=p.base_price,
                    variant_price_snapshot=(v.price_delta if v else 0),
                )

                for opt, include, delta, qty_mod in mods_materialized:
                    OrderItemModifier.objects.create(
                        order_item=oi,
                        option=opt,
                        include=include,
                        price_delta=delta,
                        qty=qty_mod,
                    )

                for _ in range(qty):
                    kt = KitchenTicket.objects.create(
                        order=order,
                        item=oi,
                        station=station,
                        status="in_progress",
                        started_at=timezone.now(),
                    )

                    broadcasts.append({
                        "station": station,
                        "type": "kitchen.ticket",
                        "data": {
                            "id": kt.id,
                            "order_number": order.number,
                            "station": station,
                            "status": kt.status,
                            "product_name": oi.product_name_snapshot,
                            "variant_name": oi.variant_name_snapshot,
                            "qty": 1,  # 🔒 ALWAYS 1
                            "modifiers": ticket_mods,
                            "table_name": order.table.name if order.table else "",
                            "started_at": kt.started_at.isoformat(),
                        },
                    })


            # --- recompute totals after all items added ---
            order.recompute_totals(tax_rate=tax_rate, save=True)
            transaction.on_commit(lambda: self._broadcast_new_items(broadcasts))

        return Response(OrderOutSer(order).data)



    @action(detail=True, methods=["get", "post"], url_path="comps")
    def comps(self, request, pk=None):
        order = self.get_object()

        if request.method == "GET":
            return Response(OrderCompOutSer(order.comps.order_by("-id"), many=True).data)

        # POST (create comp)
        ser = OrderCompInSer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        scope, mode = data["scope"], data["mode"]
        reason = data.get("reason", "") or ""

        with transaction.atomic():
            order = Order.objects.select_for_update().get(pk=order.pk)
            comp_kwargs = {
                "order": order, "scope": scope, "mode": mode,
                "reason": reason, "created_by": getattr(request, "user", None),
            }

            if scope == "item":
                item = (OrderItem.objects.select_for_update()
                        .filter(pk=data["item_id"], order=order).first())
                if not item:
                    return Response({"detail": "Item not found on this order."}, status=404)

                remaining = self._item_remaining_comp(item)
                if remaining <= 0:
                    return Response({"detail": "This item has no remaining amount to comp."}, status=400)

                comp_kwargs["item"] = item
                comp_kwargs["unit_price_snapshot"] = item.unit_price

                if mode == "qty":
                    qty = int(data["qty"])
                    if qty <= 0:
                        return Response({"detail": "qty must be > 0."}, status=400)
                    raw_amount = (item.unit_price or Decimal("0.00")) * Decimal(qty)
                    amount = min(remaining, raw_amount)
                    comp_kwargs["qty"] = qty

                elif mode == "percent":
                    pct = Decimal(data["percent"])
                    if pct < 0:
                        return Response({"detail": "percent must be >= 0."}, status=400)
                    raw_amount = (item.line_total or Decimal("0.00")) * (pct / Decimal("100.00"))
                    amount = min(remaining, raw_amount.quantize(Decimal("0.01")))
                    comp_kwargs["percent"] = pct

                elif mode == "amount":
                    raw = Decimal(data["amount"])
                    if raw < 0:
                        return Response({"detail": "amount must be >= 0."}, status=400)
                    amount = min(remaining, raw.quantize(Decimal("0.01")))
                else:
                    return Response({"detail": "Invalid mode for item comp."}, status=400)

            else:  # scope == "order"
                remaining = self._order_remaining_comp(order)
                if remaining <= 0:
                    return Response({"detail": "This order has no remaining amount to comp."}, status=400)
                if mode == "qty":
                    return Response({"detail": "mode='qty' is only valid for scope='item'."}, status=400)

                if mode == "percent":
                    pct = Decimal(data["percent"])
                    if pct < 0:
                        return Response({"detail": "percent must be >= 0."}, status=400)
                    raw_amount = remaining * (pct / Decimal("100.00"))
                    amount = min(remaining, raw_amount.quantize(Decimal("0.01")))
                    comp_kwargs["percent"] = pct
                elif mode == "amount":
                    raw = Decimal(data["amount"])
                    if raw < 0:
                        return Response({"detail": "amount must be >= 0."}, status=400)
                    amount = min(remaining, raw.quantize(Decimal("0.01")))
                else:
                    return Response({"detail": "Invalid mode for order comp."}, status=400)

            comp_kwargs["amount"] = amount
            OrderComp.objects.create(**comp_kwargs)

            order.recompute_totals(save=True)

        fresh = (Order.objects
                .prefetch_related("items__product", "items__variant",
                                "items__modifiers__option", "tickets",
                                "comps", "items__comps")
                .get(pk=order.pk))
        return Response(OrderOutSer(fresh, context={"request": request}).data, status=201)


    def _broadcast_new_items(self, broadcasts):
        try:
            layer = get_channel_layer()
            for b in broadcasts:
                msg_type = b.get("type", "kitchen.ticket")
                async_to_sync(layer.group_send)(
                    f"kitchen.{b['station']}",
                    {"type": msg_type, "data": b["data"]},
                )
        except Exception:
            pass

    # -----------------------------
    # Settle
    # -----------------------------
    @action(detail=True, methods=["post"])
    def settle(self, request, pk=None):
        ser = OrderSettleSer(data=request.data)
        ser.is_valid(raise_exception=True)
        payment_method = ser.validated_data.get("payment_method", None)
        tax_rate = ser.validated_data.get("tax_rate", None)

        free_table_param = ser.validated_data.get("free_table", None)
        do_free = True if free_table_param is None else bool(free_table_param)

        with transaction.atomic():
            order = Order.objects.select_for_update().get(pk=pk)
            if order.paid_at:
                return Response({"detail": "Order already settled."}, status=400)

            was_tab = (order.status == "tab")  # track before settle

            # keep totals fresh (comp-aware)
            order.recompute_totals(tax_rate=tax_rate, save=True)

            # settle
            order.settle(payment_method=payment_method or "cash", free_table=do_free)

            # NEW: stamp who did the settlement (and who closed the tab)
            updates = []
            if getattr(request, "user", None):
                if not getattr(order, "paid_by_id", None):
                    order.paid_by = request.user
                    updates.append("paid_by")
                if was_tab:
                    order.tab_closed_by = request.user
                    updates.append("tab_closed_by")
            if updates:
                order.save(update_fields=updates)

        order.refresh_from_db()
        return Response(OrderOutSer(order, context={"request": request}).data, status=status.HTTP_200_OK)


    @action(detail=True, methods=["post"])
    def open_tab(self, request, pk=None):
        ser = OrderOpenTabSer(data=request.data)
        ser.is_valid(raise_exception=True)

        with transaction.atomic():
            order = Order.objects.select_for_update().get(pk=pk)
            if order.status in ("paid", "void"):
                return Response({"detail": "Order can’t be put on tab."}, status=400)

            order.open_tab(
                customer_name=ser.validated_data.get("customer_name", ""),
                remark=ser.validated_data.get("remark", ""),
                credit_given=ser.validated_data.get("credit_given", None),
            )

            # NEW: stamp who opened the tab
            if getattr(request, "user", None) and not getattr(order, "tab_opened_by_id", None):
                order.tab_opened_by = request.user
                order.save(update_fields=["tab_opened_by"])

            if "credit_given" in ser.validated_data:
                order.credit_given = bool(ser.validated_data["credit_given"])
                order.save(update_fields=["credit_given"])

        return Response(OrderOutSer(order, context={"request": request}).data)

    @action(detail=True, methods=["patch", "delete"], url_path=r"items/(?P<item_id>\d+)")
    def modify_item(self, request, pk=None, item_id=None):
        order = self.get_object()

        with transaction.atomic():
            item = (
                OrderItem.objects
                .select_for_update()
                .filter(pk=item_id, order=order)
                .first()
            )
            if not item:
                return Response({"detail": "Item not found"}, status=404)

            active_tickets = list(
                KitchenTicket.objects.filter(
                    item=item,
                    status__in=["queued", "in_progress"]
                ).order_by("id")
            )

            station = active_tickets[0].station if active_tickets else None

            # ---------------- DELETE LINE ----------------
            if request.method.lower() == "delete":
                now = timezone.now()
                for kt in active_tickets:
                    kt.status = "done"
                    kt.started_at = kt.started_at or kt.created_at
                    kt.done_at = now
                    kt.save(update_fields=["status", "started_at", "done_at"])

                item.delete()
                order.recompute_totals(save=True)

                if station:
                    transaction.on_commit(lambda: [
                        self._group_send(
                            station,
                            {"type": "kitchen.cancel", "data": {"id": kt.id}}
                        ) for kt in active_tickets
                    ])

                return Response(OrderOutSer(order).data)

            # ---------------- PATCH QTY ----------------
            try:
                new_qty = int(request.data.get("qty"))
            except Exception:
                return Response({"detail": "qty must be integer"}, status=400)

            current_qty = item.qty

            # ---- qty <= 0 → delete ----
            if new_qty <= 0:
                now = timezone.now()
                for kt in active_tickets:
                    kt.status = "done"
                    kt.done_at = now
                    kt.save(update_fields=["status", "done_at"])

                item.delete()
                order.recompute_totals(save=True)
                return Response(OrderOutSer(order).data)

            # ---- qty increased → CREATE NEW TICKETS ----
            if new_qty > current_qty:
                diff = new_qty - current_qty
                for _ in range(diff):
                    kt = KitchenTicket.objects.create(
                        order=order,
                        item=item,
                        station=station,
                        status="in_progress",
                        started_at=timezone.now(),
                    )

                    transaction.on_commit(lambda kt=kt: self._group_send(
                        station,
                        {
                            "type": "kitchen.ticket",
                            "data": KitchenTicketOutSer(kt).data
                        }
                    ))

            # ---- qty decreased → CLOSE EXTRA TICKETS ----
            elif new_qty < current_qty:
                to_close = active_tickets[new_qty:]
                now = timezone.now()

                for kt in to_close:
                    kt.status = "done"
                    kt.done_at = now
                    kt.save(update_fields=["status", "done_at"])

                    transaction.on_commit(lambda kid=kt.id: self._group_send(
                        station,
                        {"type": "kitchen.cancel", "data": {"id": kid}}
                    ))

            # ---- update order item only ----
            item.qty = new_qty
            item.recompute(save=False)
            item.save(update_fields=["qty", "line_total"])
            order.recompute_totals(save=True)

        return Response(OrderOutSer(order).data)


    @action(detail=False, methods=["get"])
    def tabs(self, request):
        q = (self.request.query_params.get("q") or "").strip()
        qs = Order.objects.filter(status="tab").order_by("-tab_opened_at", "-id")

        if q:
            qs = qs.filter(
                Q(customer_name__icontains=q) |
                Q(table__name__icontains=q) |
                Q(number__icontains=q)
            )

        return Response(OrderOutSer(qs, many=True, context={"request": request}).data)


    @action(detail=False, methods=["get"], url_path="tabs/paid")
    def tabs_paid(self, request):
        qs = Order.objects.filter(status="paid", tab_opened_at__isnull=False)
        return Response(OrderOutSer(qs, many=True, context={"request": request}).data)


    def _item_remaining_comp(self, item) -> Decimal:
        used = (
            OrderComp.objects
            .filter(item=item, voided_at__isnull=True)
            .aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
        )
        return max((item.line_total or Decimal("0.00")) - used, Decimal("0.00"))


    def _order_remaining_comp(self, order) -> Decimal:
        gross = (
            OrderItem.objects
            .filter(order=order)
            .aggregate(s=Sum("line_total"))["s"] or Decimal("0.00")
        )
        used = (
            OrderComp.objects
            .filter(order=order, voided_at__isnull=True)
            .aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
        )
        return max(gross - used, Decimal("0.00"))

    def get_queryset(self):
        qs = Order.objects.all().order_by("-id")

        if self.action == "list":
            q = (self.request.query_params.get("q") or "").strip()
            if q:
                qs = qs.filter(
                    Q(number__icontains=q) |
                    Q(customer_name__icontains=q) |
                    Q(table__name__icontains=q)
                )
        return qs





class KitchenTicketViewSet(mixins.ListModelMixin,
                           mixins.UpdateModelMixin,
                           viewsets.GenericViewSet):
    serializer_class = KitchenTicketOutSer


    def get_queryset(self):
        from django.utils import timezone
        from datetime import datetime

        qs = (
            KitchenTicket.objects
            .exclude(status="cancelled")  # ⬅️ ALWAYS HIDE CANCELLED TICKETS
            .select_related("order", "item__product", "item__variant")
            .prefetch_related("item__modifiers__option")
            .order_by("created_at")
        )

        # ----------------------------------------------------
        # 🔥 DATE FILTER SUPPORT (?date=YYYY-MM-DD)
        # ----------------------------------------------------
        date_str = self.request.query_params.get("date")
        if date_str:
            try:
                target_day = datetime.fromisoformat(date_str).date()

                # Local Thai start-of-day
                local_start = timezone.make_aware(
                    datetime.combine(target_day, datetime.min.time())
                )
                local_end = timezone.make_aware(
                    datetime.combine(target_day, datetime.max.time())
                )

                # Convert to UTC (DB stores UTC)
                utc_start = local_start.astimezone(timezone.utc)
                utc_end = local_end.astimezone(timezone.utc)

                qs = qs.filter(
                    created_at__gte=utc_start,
                    created_at__lte=utc_end,
                )
            except Exception:
                pass

        # ----------------------------------------------------
        # Station + Status Filters
        # ----------------------------------------------------
        station = self.request.query_params.get("station")
        status_q = self.request.query_params.get("status")

        if station:
            qs = qs.filter(station__iexact=station)

        if status_q:
            qs = qs.filter(status=status_q)

        return qs



    def get_permissions(self):
        if self.action == "list":
            return [AllowAny()]
        if self.action in ("partial_update", "update"):
            return [IsAuthenticated(), InGroups("Kitchen", "Manager")()]
        return [IsAuthenticated()]

    def partial_update(self, request, pk=None, *args, **kwargs):
        from django.utils import timezone

        kt = self.get_queryset().get(pk=pk)
        new_status = request.data.get("status")

        if new_status not in dict(KitchenTicket.STATUS):
            return Response({"detail": "invalid status"}, status=400)

        if new_status == kt.status:
            return Response(KitchenTicketOutSer(kt).data)

        now = timezone.now()
        update_fields = ["status"]

        if new_status == "in_progress" and not kt.started_at:
            kt.started_at = now
            update_fields.append("started_at")
        elif new_status == "done":
            if not kt.started_at:
                kt.started_at = kt.created_at
                update_fields.append("started_at")
            if not kt.done_at:
                kt.done_at = now
                update_fields.append("done_at")

        kt.status = new_status
        kt.save(update_fields=update_fields)

        # Broadcast to WS
        try:
            layer = get_channel_layer()
            async_to_sync(layer.group_send)(
                f"kitchen.{kt.station}",
                {
                    "type": "kitchen.update",
                    "data": {
                        "id": kt.id,
                        "status": kt.status,
                        "started_at": kt.started_at.isoformat() if kt.started_at else None,
                        "done_at": kt.done_at.isoformat() if kt.done_at else None,
                    },
                },
            )
        except Exception:
            pass

        return Response(KitchenTicketOutSer(kt).data)


from datetime import datetime, timedelta, time
from decimal import Decimal
from django.db import transaction
from django.db.models import Sum, F, Count
from django.db.models.functions import TruncHour
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import api_view, permission_classes, action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from catalog.models import Product, ProductVariant, ModifierOption
from .models import Order, OrderItem, KitchenTicket, Table, OrderItemModifier
from .permissions import InGroups

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def report_daily(request):
    # 1) window for the store's local day → UTC
    utc_start, utc_end = local_day_window_utc(request.GET.get("date"))

    # 2) Define the "day" by paid_at (cashier day)
    day_filter = Q(paid_at__gte=utc_start, paid_at__lt=utc_end)

    # 3) All orders whose PAID time falls in this window (includes voided if any had paid_at)
    qs_orders = Order.objects.filter(day_filter)

    # Only actually paid sales
    qs_sales = qs_orders.filter(status="paid")

    # Counts & totals (net totals after comps)
    orders_count = qs_sales.count()
    orders_total = qs_sales.aggregate(s=Coalesce(Sum("total"), DEC0))["s"] or Decimal("0.00")

    # Voids summary (most voids won't have paid_at; keep for completeness)
    qs_voided = qs_orders.filter(status="void")
    voided_count = qs_voided.count()
    voided_total = qs_voided.aggregate(s=Coalesce(Sum("total"), DEC0))["s"] or Decimal("0.00")

    # Payments (paid only)
    by_payment = (
        qs_sales.values("payment_method")
        .annotate(
            count=Count("id"),
            total=Coalesce(Sum("total"), DEC0),
        )
        .order_by("payment_method")
    )

    # ---------- FOC / comps ----------
    qs_comps = (
        OrderComp.objects
        .filter(order__in=qs_orders)                 # same paid_at basis through related orders
        .select_related("order")
    )
    qs_comps_active = qs_comps.filter(voided_at__isnull=True)

    foc_total_raw = qs_comps_active.aggregate(s=Coalesce(Sum("amount"), DEC0))["s"] or Decimal("0.00")
    foc_total = float(abs(foc_total_raw))

    # Grab raw comps with their order info
    comps_raw = list(
        qs_comps.values(
            "id",
            "scope",
            "mode",
            "item_id",
            "percent",
            "qty",
            "amount",
            "reason",
            "voided_at",
            "order_id",
            order_number=F("order__number"),
        )
    )

    # Resolve item_id -> product/variant names (only for item-scope comps) using snapshots
    item_ids = [r["item_id"] for r in comps_raw if r["item_id"] and r["scope"] == "item"]
    item_map = {}
    if item_ids:
        for row in (
            OrderItem.objects
            .select_related("product", "variant")
            .filter(id__in=item_ids)
            .values("id",
                    product_name=F("product_name_snapshot"),
                    variant_name=F("variant_name_snapshot"))
        ):
            item_map[row["id"]] = {
                "product_name": row["product_name"],
                "variant_name": row["variant_name"],
            }

    # Build final comps payload including item_name / item_variant
    comps_list = []
    for r in comps_raw:
        item_name = None
        item_variant = None
        if r["scope"] == "item" and r["item_id"] in item_map:
            item_name = item_map[r["item_id"]]["product_name"]
            item_variant = item_map[r["item_id"]]["variant_name"]

        comps_list.append(
            {
                "id": r["id"],
                "scope": r["scope"],
                "mode": r["mode"],
                "item_id": r["item_id"],
                "percent": r["percent"],
                "qty": r["qty"],
                "amount": float(r["amount"] or 0),
                "reason": r["reason"],
                "voided_at": r["voided_at"],
                "order_id": r["order_id"],
                "order_number": r["order_number"],
                "item_name": item_name,       # snapshot-based
                "item_variant": item_variant, # snapshot-based
            }
        )

    # ---------- Items sold (paid only) ----------
    items_qs = (
        OrderItem.objects
        .filter(order__in=qs_sales)
        .values(
            product_name=F("product_name_snapshot"),
            variant_name=F("variant_name_snapshot"),
        )
        .annotate(
            qty=Coalesce(Sum("qty"), V(0)),
            sales=Coalesce(Sum("line_total"), DEC0),
        )
        .order_by("-sales")
    )
    items = [
        {
            "product_name": r["product_name"],
            "variant_name": r["variant_name"],
            "qty": float(r["qty"] or 0),
            "sales": float(r["sales"] or 0),
        }
        for r in items_qs
    ]

    data = {
        "orders": orders_count,
        "total": float(orders_total),
        "voided": {"count": voided_count, "total": float(voided_total)},
        "by_payment": [
            {"payment_method": p["payment_method"], "count": p["count"], "total": float(p["total"] or 0)}
            for p in by_payment
        ],
        "comps": comps_list,
        "foc_total": foc_total,
        "items": items,
        "hourly": [],
        "by_category": [],
    }
    return Response(data)


# -----------------------------
# Tables
# -----------------------------
# orders/views.py  (add inside TableViewSet)

from decimal import Decimal
from django.db import transaction
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

class TableViewSet(mixins.ListModelMixin,
                   mixins.CreateModelMixin,
                   mixins.UpdateModelMixin,
                   mixins.DestroyModelMixin,
                   viewsets.GenericViewSet):
    
    queryset = Table.objects.all().order_by("sort", "name")
    serializer_class = TableSer


    def get_permissions(self):
        if self.action == "list":
            return [AllowAny()]
        if self.action in ("create", "update", "partial_update", "destroy",
                           "free", "occupy", "close", "open", "bulk_create",
                           "reorder", "rename"):
            return [IsAuthenticated(), InGroups("Manager")()]
        if self.action == "active_order":
            if self.request.method == "GET":
                return [IsAuthenticated()]
            return [IsAuthenticated(), InGroups("Manager", "Cashier")()]
        return [IsAuthenticated()]


    @action(detail=True, methods=["get", "post"], url_path="active_order")
    def active_order(self, request, pk=None):
        """
        GET  /api/tables/{id}/active_order/  → return the latest non-paid/non-void order for this table
        POST /api/tables/{id}/active_order/  → create (or return) an active order and mark table occupied (except Takeaway)
        """
        table = self.get_object()

        order = (
            Order.objects
            .filter(table=table)
            .exclude(status__in=["paid", "void", "tab"])   # <-- add "tab" here
            .order_by("-id")
            .first()
        )

        # ---- GET: just read it
        if request.method == "GET":
            if not order:
                return Response({"detail": "active order not found"}, status=404)
            return Response(OrderOutSer(order, context={"request": request}).data)

        # ---- POST: create if missing
        if order:
            return Response(OrderOutSer(order, context={"request": request}).data)

        with transaction.atomic():
            order = Order.objects.create(
                table=table,
                status="open",
                subtotal=Decimal("0.00"),
                tax=Decimal("0.00"),
                total=Decimal("0.00"),
            )
            # occupy the table (except Takeaway)
            if table.name.lower() != "takeaway" and table.status != "occupied":
                table.status = "occupied"
                table.save(update_fields=["status"])

        return Response(
            OrderOutSer(order, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )
    
    # ---------- CREATE ----------
    def create(self, request, *args, **kwargs):
        ser = TableAdminInSer(data=request.data)
        ser.is_valid(raise_exception=True)
        obj = Table.objects.create(**ser.validated_data)
        return Response(TableSer(obj).data, status=status.HTTP_201_CREATED)

    # ---------- UPDATE (rename / sort / status) ----------
    def partial_update(self, request, *args, **kwargs):
        obj = self.get_object()
        ser = TableAdminInSer(obj, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(TableSer(obj).data)

    # convenience alias
    @action(detail=True, methods=["post"])
    def rename(self, request, pk=None):
        obj = self.get_object()
        new_name = (request.data.get("name") or "").strip()
        if not new_name:
            return Response({"detail": "Name required."}, status=400)
        if Table.objects.exclude(pk=obj.pk).filter(name__iexact=new_name).exists():
            return Response({"detail": "Name already exists."}, status=400)
        obj.name = new_name
        obj.save(update_fields=["name"])
        return Response(TableSer(obj).data)

    # ---------- REORDER ----------
    @action(detail=False, methods=["post"])
    def reorder(self, request):
        """
        Body: [{id: 3, sort: 10}, {id: 7, sort: 20}, ...]
        """
        rows = request.data if isinstance(request.data, list) else []
        ids = [r.get("id") for r in rows if "id" in r]
        if not ids:
            return Response({"detail": "No rows."}, status=400)
        by_id = {r["id"]: int(r.get("sort", 0)) for r in rows}
        for t in Table.objects.filter(id__in=ids):
            t.sort = by_id.get(t.id, t.sort)
            t.save(update_fields=["sort"])
        out = TableSer(Table.objects.all().order_by("sort", "name"), many=True).data
        return Response(out)

    # ---------- CLOSE / OPEN ----------
    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        t = self.get_object()
        t.status = "closed"
        t.save(update_fields=["status"])
        return Response(TableSer(t).data)
    
    @action(detail=True, methods=["post"])
    def open(self, request, pk=None):
        t = self.get_object()
        t.status = "free"
        t.save(update_fields=["status"])
        return Response(TableSer(t).data)
    
    # keep your existing free/occupy actions for quick toggles
    @action(detail=True, methods=["post"])
    def free(self, request, pk=None):
        with transaction.atomic():
            t = Table.objects.select_for_update().get(pk=pk)
            active_orders = list(
                Order.objects.select_for_update().filter(table=t, status="open")
            )

            blocking_order_ids = []
            removable_order_ids = []

            for order in active_orders:
                has_activity = (
                    order.items.exists()
                    or order.tickets.exists()
                    or order.comps.filter(voided_at__isnull=True).exists()
                    or Decimal(order.subtotal or 0) > 0
                    or Decimal(order.tax or 0) > 0
                    or Decimal(order.total or 0) > 0
                )
                if has_activity:
                    blocking_order_ids.append(order.id)
                else:
                    removable_order_ids.append(order.id)

            if blocking_order_ids:
                return Response(
                    {"detail": "Cannot mark table free while an active order exists."},
                    status=400,
                )

            if removable_order_ids:
                Order.objects.filter(id__in=removable_order_ids).delete()

            if t.status != "free":
                t.status = "free"
                t.save(update_fields=["status"])

        return Response(TableSer(t).data)

    @action(detail=True, methods=["post"])
    def occupy(self, request, pk=None):
        t = self.get_object()
        if t.status != "occupied":
            t.status = "occupied"
            t.save(update_fields=["status"])
        return Response(TableSer(t).data)

    # ---------- DELETE ----------
    def destroy(self, request, *args, **kwargs):
        obj = self.get_object()
        # block delete if there’s an active order on this table
        has_active = Order.objects.filter(table=obj, status__in=["open"]).exists()
        if has_active:
            return Response(
                {"detail": "Cannot delete: table has an active order."},
                status=400
            )
        obj.delete()
        return Response(status=204)

    # ---------- BULK CREATE ----------
    @action(detail=False, methods=["post"])
    def bulk_create(self, request):
        """
        Body: { names: ["A1","A2","A3"], prefix_optional: "A", start_sort: 0 }
        """
        names = request.data.get("names") or []
        start_sort = int(request.data.get("start_sort") or 0)
        created = []
        sort = start_sort
        for raw in names:
            name = (str(raw) or "").strip()
            if not name:
                continue
            if Table.objects.filter(name__iexact=name).exists():
                continue
            created.append(Table(name=name, sort=sort))
            sort += 10
        if created:
            Table.objects.bulk_create(created, ignore_conflicts=True)
        out = TableSer(Table.objects.all().order_by("sort", "name"), many=True).data
        return Response({"created": len(created), "tables": out}, status=201)


from collections import Counter
import re

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from catalog.models import Product
from .models import KitchenTicket
from .serializers import _clean_station  # keep using your shared helper

_RE_SPACE = re.compile(r"\s+")

def _pretty(code: str) -> str:
    # "TESTING_STATION" -> "Testing Station"
    return re.sub(r"[_.-]+", " ", code).title()

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def kitchen_stations(request):
    """
    Returns KDS station list.
    - ONE row per unique slug
    - Count = number of OPEN tickets (queued/in_progress)
    - Add ?debug=1 to also see the raw open rows being counted
    """

    # ---------- 1) Build a UNIQUE slug -> human label map ----------
    slug_to_human = {}

    # From products: normalize to slug; choose a decent label
    for raw in Product.objects.values_list("kitchen_station", flat=True).distinct():
        human = (raw or "").strip() or "Main"
        slug = _clean_station(human)
        # prefer a pretty-cased label
        slug_to_human.setdefault(slug, _pretty(slug))

    # From tickets (slugs are already normalized in DB)
    for slug in KitchenTicket.objects.values_list("station", flat=True).distinct():
        if not slug:
            continue
        slug_to_human.setdefault(slug, _pretty(slug))

    if not slug_to_human:
        # ensure MAIN exists
        slug_to_human["MAIN"] = "Main"

    # ---------- 2) Count ONLY open tickets, by slug ----------
    # (normalize again just in case historical data slipped through)
    open_slugs = [
        _clean_station(s or "")
        for s in KitchenTicket.objects
                .filter(status__in=["queued", "in_progress"])
                .values_list("station", flat=True)
    ]
    counts = Counter(open_slugs)

    # ---------- 3) Build response: one row per slug ----------
    data = [
        {
            "slug": slug,
            "name": slug_to_human.get(slug, _pretty(slug)),
            "count": int(counts.get(slug, 0)),
        }
        for slug in sorted(slug_to_human.keys())
    ]

    # Optional debug payload so you can see exactly what rows are counted
    if request.query_params.get("debug") == "1":
        raw_rows = list(
            KitchenTicket.objects
            .filter(status__in=["queued", "in_progress"])
            .values("id", "order_id", "station", "status")
            .order_by("-id")
        )
        return Response({"stations": data, "raw": raw_rows})

    return Response({"stations": data})


# orders/views.py
from datetime import datetime, timedelta, time
from decimal import Decimal
from django.db.models import Sum, F, Count
from django.db.models.functions import TruncDate
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from .permissions import InGroups
from .models import Order, OrderItem

def _parse_date(s):
    try:
        return datetime.fromisoformat(s).date()
    except Exception:
        return None

# orders/views.py
from django.db.models import Sum, F, Count, Q, Value as V, DecimalField
from django.db.models.functions import TruncDate, Coalesce
from decimal import Decimal

DEC0 = V(Decimal("0.00"), output_field=DecimalField(max_digits=12, decimal_places=2))

# orders/views.py
from django.db.models import Sum, F, Count, Q, Value as V, DecimalField
from django.db.models.functions import TruncDate, Coalesce
from decimal import Decimal

DEC0 = V(Decimal("0.00"), output_field=DecimalField(max_digits=12, decimal_places=2))

@api_view(["GET"])
@permission_classes([InGroups("Manager")])
def report_range(request):
    """
    GET /api/reports/range/?start=YYYY-MM-DD&end=YYYY-MM-DD
    Returns:
      - totals_by_day: [{date, orders, subtotal, total}]
      - payments: [{payment_method, count, total}]
      - products_by_day: [{date, product_name, variant_name, qty, sales}]
      - top_items: [{product_name, variant_name, qty, sales}]
      - overall: {orders, subtotal, total}
      - comps + foc_total
    """
    today = timezone.localdate()
    start_d = _parse_date(request.GET.get("start")) or (today - timedelta(days=6))
    end_d   = _parse_date(request.GET.get("end"))   or today
    if end_d < start_d:
        start_d, end_d = end_d, start_d

    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(start_d, time.min), tz)
    end   = timezone.make_aware(datetime.combine(end_d + timedelta(days=1), time.min), tz)

    # Orders whose PAID time falls in range
    qs_orders = Order.objects.filter(paid_at__gte=start, paid_at__lt=end)
    sales_qs = qs_orders.filter(status="paid")

    # ---- daily totals
    daily = (
        sales_qs.annotate(d=TruncDate("paid_at", tzinfo=tz))
        .values("d")
        .annotate(orders=Count("id"), subtotal=Sum("subtotal"), total=Sum("total"))
        .order_by("d")
    )
    totals_by_day = [
        {
            "date": row["d"].isoformat(),
            "orders": int(row["orders"] or 0),
            "subtotal": float(row["subtotal"] or 0),
            "total": float(row["total"] or 0),
        }
        for row in daily
    ]

    # ---- payments
    payments = list(
        sales_qs.values("payment_method")
        .annotate(count=Count("id"), total=Sum("total"))
        .order_by("payment_method")
    )
    for p in payments:
        p["count"] = int(p["count"] or 0)
        p["total"] = float(p["total"] or 0)

    # ---- products by day (use snapshots)
    items_qs = (
        OrderItem.objects.filter(order__in=sales_qs)
        .annotate(d=TruncDate("order__paid_at", tzinfo=tz))
        .values("d",
                product_name=F("product_name_snapshot"),
                variant_name=F("variant_name_snapshot"))
        .annotate(qty=Sum("qty"), sales=Sum("line_total"))
        .order_by("d", "-sales")
    )
    products_by_day = [
        {
            "date": row["d"].isoformat(),
            "product_name": row["product_name"],
            "variant_name": row["variant_name"],
            "qty": float(row["qty"] or 0),
            "sales": float(row["sales"] or 0),
        }
        for row in items_qs
    ]

    # ---- top items over the whole range (snapshots)
    top_items_qs = (
        OrderItem.objects.filter(order__in=sales_qs)
        .values(product_name=F("product_name_snapshot"),
                variant_name=F("variant_name_snapshot"))
        .annotate(qty=Sum("qty"), sales=Sum("line_total"))
        .order_by("-sales")[:20]
    )
    top_items = [
        {
            "product_name": r["product_name"],
            "variant_name": r["variant_name"],
            "qty": float(r["qty"] or 0),
            "sales": float(r["sales"] or 0),
        } for r in top_items_qs
    ]

    # ---- overall (paid only)
    overall = sales_qs.aggregate(subtotal=Sum("subtotal"), total=Sum("total"), orders=Count("id"))

    # =========================
    # FOC / Comps (use snapshots for item names)
    # =========================
    qs_comps = (
        OrderComp.objects
        .filter(order__in=qs_orders)
        .select_related("order", "item")
    )
    qs_comps_active = qs_comps.filter(voided_at__isnull=True)
    foc_total_raw = qs_comps_active.aggregate(s=Coalesce(Sum("amount"), DEC0))["s"] or Decimal("0.00")
    foc_total = float(abs(foc_total_raw))

    comps_raw = list(
        qs_comps.values(
            "id", "scope", "mode", "item_id", "percent", "qty", "amount",
            "reason", "voided_at", "order_id",
            order_number=F("order__number"),
        )
    )
    item_ids = [r["item_id"] for r in comps_raw if r["item_id"] and r["scope"] == "item"]
    name_map = {}
    if item_ids:
        for row in (
            OrderItem.objects
            .select_related("product", "variant")
            .filter(id__in=item_ids)
            .values("id",
                    product_name=F("product_name_snapshot"),
                    variant_name=F("variant_name_snapshot"))
        ):
            name_map[row["id"]] = {
                "product_name": row["product_name"],
                "variant_name": row["variant_name"],
            }

    comps_list = []
    for r in comps_raw:
        item_name = None
        item_variant = None
        if r["scope"] == "item" and r["item_id"] in name_map:
            item_name = name_map[r["item_id"]]["product_name"]
            item_variant = name_map[r["item_id"]]["variant_name"]

        comps_list.append({
            "id": r["id"],
            "scope": r["scope"],
            "mode": r["mode"],
            "item_id": r["item_id"],
            "percent": r["percent"],
            "qty": r["qty"],
            "amount": float(r["amount"] or 0),
            "reason": r["reason"],
            "voided_at": r["voided_at"],
            "order_id": r["order_id"],
            "order_number": r["order_number"],
            "item_name": item_name,           # snapshot-based
            "item_variant": item_variant,     # snapshot-based
        })

    return Response({
        "range": {"start": start_d.isoformat(), "end": end_d.isoformat()},
        "totals_by_day": totals_by_day,
        "payments": payments,
        "products_by_day": products_by_day,
        "top_items": top_items,
        "overall": {
            "orders": int(overall["orders"] or 0),
            "subtotal": float(overall["subtotal"] or 0),
            "total": float(overall["total"] or 0),
        },
        "comps": comps_list,
        "foc_total": foc_total,
    })


from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from catalog.models import Product

def notify_kds_on_new_order(order_item):
    """Broadcast a new ticket message to the relevant KDS channel."""
    channel_layer = get_channel_layer()
    product = order_item.product
    station = getattr(product, "station", "MAIN")  # or however your product defines its station

    data = {
        "type": "ticket",   # ✅ your KDS listens for this type
        "data": {
            "id": order_item.id,
            "product_name": product.name,
            "variant_name": getattr(order_item, "variant_name", ""),
            "qty": order_item.qty,
            "status": "in_progress",
            "modifiers": [
                {
                    "option_name": m.option_name,
                    "price_delta": str(m.price_delta or 0),
                    "qty": m.qty or 1,
                    "include": m.include,
                    "option_id": m.option_id,
                }
                for m in order_item.modifiers.all()
            ],
            "table_name": getattr(order_item.order, "table_name", "Takeaway"),
        },
    }

    async_to_sync(channel_layer.group_send)(
        f"kitchen.{station.upper()}",  # same cleanStation() logic as frontend
        data,
    )
