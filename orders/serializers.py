from decimal import Decimal
from django.utils.timezone import now
from django.db import transaction
import re

from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from catalog.models import Product, ProductVariant, ModifierOption
from .models import Order, OrderItem, OrderItemModifier, KitchenTicket, Table

from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from catalog.models import Product, ProductVariant, ModifierOption, ProductModifierGroup
from .models import Order, OrderItem, OrderItemModifier, KitchenTicket, Table, OrderComp
from django.db.models import Sum




def _clean_station(name: str) -> str:
    # MAIN -> MAIN, "Noodles" -> NOODLES, "Hot Grill" -> HOT_GRILL
    return re.sub(r"[^0-9A-Za-z._-]", "_", (name or "MAIN").upper())[:80]


# orders/serializers.py (top-level, near other helpers)
def _option_allowed_for_product(product, option) -> bool:
    grp_id = getattr(option, "group_id", None)
    pid = getattr(product, "id", None)
    if not grp_id or not pid:
        return False
    # allowed iff the option's group is linked to the product
    return ProductModifierGroup.objects.filter(product_id=pid, group_id=grp_id).exists()


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



# ---------- TABLE ----------
class TableSer(serializers.ModelSerializer):
    status = serializers.SerializerMethodField()  # compute status live

    class Meta:
        model = Table
        fields = ("id", "name", "status", "sort")

    # def get_status(self, obj):
    #     # Occupied only if an OPEN order is on this table (tabs don't block seating)
    #     return "occupied" if Order.objects.filter(table=obj, status="open").exists() else "free"

    def get_status(self, obj):
        # If explicitly closed, always return closed
        if getattr(obj, "status", "") == "closed":
            return "closed"
        # Occupied only when an OPEN order exists (tabs don’t block seating)
        return "occupied" if Order.objects.filter(table=obj, status="open").exists() else "free"


# ---------- INPUT SCHEMAS ----------
class OrderItemModifierInSer(serializers.Serializer):
    option_id = serializers.IntegerField()
    include = serializers.BooleanField(default=True)
    qty = serializers.IntegerField(min_value=1, default=1)



class OrderItemInSer(serializers.Serializer):
    product_id = serializers.IntegerField()
    variant_id = serializers.IntegerField(required=False, allow_null=True)
    qty = serializers.IntegerField(min_value=1, default=1)
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    modifiers = OrderItemModifierInSer(many=True, required=False, default=list)

class OrderCreateSer(serializers.Serializer):
    payment_method = serializers.CharField(default="cash")
    tax_rate = serializers.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    items = OrderItemInSer(many=True)
    table_id = serializers.IntegerField(required=False, allow_null=True)
    pay_now = serializers.BooleanField(default=True)
    idempotency_key = serializers.CharField(required=False, allow_blank=True)

    @transaction.atomic
    def create(self, validated):
        tax_rate = validated["tax_rate"]
        pay_now = validated.get("pay_now", True)
        idem_key = validated.get("idempotency_key")

        # Idempotency (optional): reuse an existing order if number matches
        if idem_key:
            existing = Order.objects.filter(number=idem_key).first()
            if existing:
                return existing

        # Create order (allow forcing number via idempotency key)
        order = Order.objects.create(
            number=idem_key or None,
            payment_method=validated["payment_method"],
        )

        # Attach table (mark occupied unless Takeaway)
        table_obj = None
        tid = validated.get("table_id")
        if tid:
            try:
                table_obj = Table.objects.select_for_update().get(pk=tid)
            except Table.DoesNotExist:
                raise ValidationError({"table_id": "Invalid table."})
            order.table = table_obj
            order.table_name_snapshot = table_obj.name
            if table_obj.name.lower() != "takeaway" and table_obj.status != "occupied":
                table_obj.status = "occupied"
                table_obj.save(update_fields=["status"])

        subtotal = Decimal("0.00")
        layer = get_channel_layer()

        for it in validated["items"]:
            # ---- product (must be ACTIVE) ----
            try:
                p = Product.objects.get(pk=it["product_id"], is_active=True)
            except Product.DoesNotExist:
                raise ValidationError({"items": "Invalid or inactive product."})

            # ---- tolerant variant resolution ----
            v = None
            base = Decimal(p.base_price)

            v_id = it.get("variant_id")
            if v_id:
                # 1) try active by id
                v = ProductVariant.objects.filter(pk=v_id, product=p, is_active=True).first()
                if not v:
                    # 2) id exists but inactive? remap by name to an ACTIVE variant
                    old = ProductVariant.objects.filter(pk=v_id, product=p).first()
                    if old and old.name:
                        v = ProductVariant.objects.filter(
                            product=p, name__iexact=old.name, is_active=True
                        ).first()
                # 3) if still not found, fall back to no variant (base only)

            if v:
                base += Decimal(v.price_delta)

            qty = int(it["qty"])
            notes = it.get("notes", "") or ""

            # ---- modifiers: validate + materialize (do NOT write yet) ----
            total_delta = Decimal("0.00")
            mods_materialized = []  # (opt, include, delta)
            ticket_mods = []
            for m in it.get("modifiers", []) or []:
                try:
                    opt = ModifierOption.objects.get(pk=m["option_id"])
                except ModifierOption.DoesNotExist:
                    raise ValidationError({"items": "Invalid option_id in modifiers."})

                if not _option_allowed_for_product(p, opt):
                    raise ValidationError({
                        "items": f"Modifier option #{opt.id} ({opt.name}) is not allowed "
                                 f"for product #{p.id} ({p.name})."
                    })

                include = bool(m.get("include", True))
                qty_mod = int(m.get("qty", 1)) or 1
                delta = Decimal(opt.price_delta) if include else Decimal("0.00")
                mods_materialized.append((opt, include, delta, qty_mod))  
                total_delta += delta * qty_mod    

                ticket_mods.append({
                    "option_id": opt.id,
                    "option_name": opt.name,
                    "group_name": getattr(opt.group, "name", "Options"),
                    "show_title": _modifier_show_title(p.id, opt.group_id),
                    "include": include,
                    "price_delta": str(delta),
                    "qty": qty_mod,
                })

            unit = base + total_delta

            # ---- create order item with immutable name snapshots ----
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
            )

            # ---- now create modifiers linked to this item ----
            for opt, include, delta, qty_mod in mods_materialized:
                OrderItemModifier.objects.create(
                    order_item=oi,
                    option=opt,
                    include=include,
                    price_delta=delta,
                    qty=qty_mod,
                )

            subtotal += oi.line_total

            # ---- kitchen ticket + broadcast (use snapshots) ----
            station = _clean_station(p.kitchen_station)
            # kt = KitchenTicket.objects.create(
            #     order=order,
            #     item=oi,
            #     station=station,
            #     status="in_progress",
            #     started_at=now(),
            # )


            for _ in range(qty):
                kt = KitchenTicket.objects.create(
                    order=order,
                    item=oi,
                    station=station,
                    status="in_progress",
                    started_at=now(),
                )

                if layer is not None:
                    async_to_sync(layer.group_send)(
                        f"kitchen.{station}",
                        {
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
                        },
                    )


            # if layer is not None:
            #     payload = {
            #         "id": kt.id,
            #         "order_number": order.number,
            #         "station": station,
            #         "status": kt.status,
            #         "product_name": oi.product_name_snapshot,   # snapshot
            #         "variant_name": oi.variant_name_snapshot,   # snapshot
            #         "qty": qty,
            #         "modifiers": ticket_mods,
            #         "table_name": order.table.name if order.table else "",
            #         "started_at": kt.started_at.isoformat() if kt.started_at else None,
            #         "done_at": kt.done_at.isoformat() if kt.done_at else None,
            #     }
            #     async_to_sync(layer.group_send)(
            #         f"kitchen.{station}",
            #         {"type": "kitchen.ticket", "data": payload},
            #     )

        # totals
        tax = (subtotal * tax_rate / Decimal("100.00")).quantize(Decimal("0.01"))
        order.subtotal = subtotal
        order.tax = tax
        order.total = subtotal + tax

        if pay_now:
            order.paid_at = now()
            order.status = "paid"

        order.save(update_fields=["table", "table_name_snapshot", "subtotal", "tax", "total", "paid_at", "status"])

        # free table immediately if dine-in and pay_now
        if pay_now and order.table and order.table.name.lower() != "takeaway":
            t = order.table
            if t.status != "free":
                t.status = "free"
                t.save(update_fields=["status"])

        return order

# ---------- EXTRA INPUTS FOR OPEN-TAB FLOWS ----------
class OrderAddItemsSer(serializers.Serializer):
    tax_rate = serializers.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    items = OrderItemInSer(many=True)


class OrderSettleSer(serializers.Serializer):
    payment_method = serializers.ChoiceField(choices=["cash", "card", "transfer"], required=False)
    tax_rate = serializers.DecimalField(max_digits=5, decimal_places=2, required=False)
    free_table = serializers.BooleanField(required=False)


class OrderOpenTabSer(serializers.Serializer):
    customer_name = serializers.CharField(required=False, allow_blank=True, max_length=120)
    remark = serializers.CharField(required=False, allow_blank=True, default="")
    credit_given = serializers.BooleanField(required=False)

# ---------- OUTPUT SCHEMAS ----------
class OrderItemModifierOutSer(serializers.ModelSerializer):
    option_name = serializers.CharField(source="option.name", read_only=True)
    option_id = serializers.IntegerField(source="option.id", read_only=True)
    group_name = serializers.SerializerMethodField()
    show_title = serializers.SerializerMethodField()

    def get_group_name(self, obj):
        try:
            return obj.option.group.name
        except Exception:
            return None

    def get_show_title(self, obj):
        try:
            return _modifier_show_title(obj.order_item.product_id, obj.option.group_id)
        except Exception:
            return True

    class Meta:
        model = OrderItemModifier
        fields = ("option_id", "option_name", "group_name", "show_title", "include", "price_delta", "qty")

class OrderItemOutSer(serializers.ModelSerializer):
    # 🔒 read from immutable snapshots, not live related names
    product_name = serializers.CharField(source="product_name_snapshot", read_only=True)
    variant_name = serializers.CharField(source="variant_name_snapshot", read_only=True)
    modifiers = OrderItemModifierOutSer(many=True, read_only=True)
    total_modifiers = serializers.SerializerMethodField()  # ✅ new field

    # NEW
    comped_amount = serializers.SerializerMethodField()
    net_line_total = serializers.SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = (
            "id",
            "product_name",
            "variant_name",
            "qty",
            "unit_price",
            "line_total",
            "modifiers",
            "total_modifiers",  # ✅ include it here
            "notes",
            "comped_amount",
            "net_line_total",
        )

    def get_total_modifiers(self, obj):
        total = Decimal("0.00")
        for m in obj.modifiers.all():
            total += (m.price_delta or Decimal("0.00")) * (m.qty or 1)
        return total

    def get_comped_amount(self, obj):
        cached = getattr(obj, "_comped_amount", None)
        if cached is not None:
            return Decimal(cached)
        val = (
            OrderComp.objects
            .filter(item=obj, voided_at__isnull=True)
            .aggregate(s=Sum("amount"))
            .get("s") or Decimal("0.00")
        )
        return val

    def get_net_line_total(self, obj):
        comp = self.get_comped_amount(obj)
        net = (obj.line_total or Decimal("0.00")) - comp
        return net if net > 0 else Decimal("0.00")



class OrderCompOutSer(serializers.ModelSerializer):
    item_id = serializers.IntegerField(read_only=True)
    created_by_name = serializers.CharField(source="created_by.username", read_only=True)
    voided_by_name  = serializers.CharField(source="voided_by.username",  read_only=True)

    class Meta:
        model = OrderComp
        fields = [
            "id", "scope", "mode", "item_id",
            "qty", "percent", "unit_price_snapshot",
            "amount", "reason",
            "created_by", "created_by_name", "created_at",
            "voided_at", "voided_by", "voided_by_name",
        ]


class OrderCompInSer(serializers.Serializer):
    scope   = serializers.ChoiceField(choices=OrderComp.SCOPE_CHOICES)   # "order" | "item"
    mode    = serializers.ChoiceField(choices=OrderComp.MODE_CHOICES)    # "qty" | "amount" | "percent"
    item_id = serializers.IntegerField(required=False, allow_null=True)

    qty     = serializers.IntegerField(required=False)
    amount  = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    percent = serializers.DecimalField(max_digits=6,  decimal_places=2, required=False)

    reason  = serializers.CharField(required=False, allow_blank=True)

    def validate(self, data):
        scope, mode = data["scope"], data["mode"]
        item_id = data.get("item_id")

        if scope == "item" and not item_id:
            raise ValidationError("item_id is required for scope='item'.")
        if scope == "order" and item_id:
            raise ValidationError("item_id is not allowed for scope='order'.")

        need = {"qty": "qty", "amount": "amount", "percent": "percent"}[mode]
        if data.get(need) is None:
            raise ValidationError(f"{need} is required for mode='{mode}'.")

        if mode == "qty" and data["qty"] <= 0:
            raise ValidationError("qty must be > 0.")
        if mode == "amount" and data["amount"] < 0:
            raise ValidationError("amount must be >= 0.")
        if mode == "percent" and data["percent"] < 0:
            raise ValidationError("percent must be >= 0.")
        return data




from rest_framework import serializers

class OrderOutSer(serializers.ModelSerializer):
    items = OrderItemOutSer(many=True, read_only=True)
    table = TableSer(read_only=True)
    is_paid = serializers.SerializerMethodField()

    # breakdown + comps
    gross_subtotal = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    comps_total    = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    comps          = OrderCompOutSer(many=True, read_only=True)

    # NEW: who did what (robust to nulls)
    paid_by_name       = serializers.SerializerMethodField()
    tab_opened_by_name = serializers.SerializerMethodField()
    tab_closed_by_name = serializers.SerializerMethodField()

    table_display = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = (
            "id",
            "number",
            "created_at",
            "paid_at",
            "status",
            "subtotal",     # net after comps
            "tax",
            "total",
            "payment_method",
            "is_paid",
            "table",
            "table_display",
            "items",

            # breakdown + comps
            "gross_subtotal",
            "comps_total",
            "comps",

            # customer / tab
            "customer_name",
            "credit_remark",
            "credit_given",
            "tab_opened_at",
            "tab_closed_at",

            # NEW: actors (ids + display names)
            "paid_by",               # model PK field (read-only by default in list)
            "paid_by_name",
            "tab_opened_by",
            "tab_opened_by_name",
            "tab_closed_by",
            "tab_closed_by_name",
        )

    def get_table_display(self, obj):
        try:
            live = obj.table.name if obj.table else ""
        except Exception:
            live = ""
        return live or (obj.table_name_snapshot or "")

    # ---------- helpers ----------
    def _display_name(self, user):
        """Prefer full name, then username, then email. Handle None safely."""
        if not user:
            return None
        # try Django's get_full_name first if available
        try:
            full = user.get_full_name().strip()
        except Exception:
            full = ""
        if full:
            return full
        # fallbacks
        return getattr(user, "username", None) or getattr(user, "email", None)

    # ---------- S.M. fields ----------
    def get_paid_by_name(self, obj):
        return self._display_name(getattr(obj, "paid_by", None))

    def get_tab_opened_by_name(self, obj):
        return self._display_name(getattr(obj, "tab_opened_by", None))

    def get_tab_closed_by_name(self, obj):
        return self._display_name(getattr(obj, "tab_closed_by", None))

    def get_is_paid(self, obj):
        return bool(obj.paid_at)



class KitchenTicketOutSer(serializers.ModelSerializer):
    order_number = serializers.CharField(source="order.number", read_only=True)
    # 🔒 snapshots for display stability
    product_name = serializers.CharField(source="item.product_name_snapshot", read_only=True)
    variant_name = serializers.CharField(source="item.variant_name_snapshot", read_only=True)
    # qty = serializers.IntegerField(source="item.qty", read_only=True)
    # qty = serializers.IntegerField(default=1, read_only=True)
    qty = serializers.SerializerMethodField()

    modifiers = OrderItemModifierOutSer(source="item.modifiers", many=True, read_only=True)
    table_name = serializers.SerializerMethodField()
    # timers for KDS
    started_at = serializers.DateTimeField(read_only=True)
    done_at = serializers.DateTimeField(read_only=True)

    def get_qty(self, obj):
        return 1

    def get_table_name(self, obj):
        order = getattr(obj, "order", None)
        if not order:
            return ""
        return getattr(getattr(order, "table", None), "name", None) or getattr(order, "table_name_snapshot", "") or ""

    class Meta:
        model = KitchenTicket
        fields = (
            "id",
            "order_number",
            "station",
            "status",
            "product_name",
            "variant_name",
            "qty",
            "modifiers",
            "table_name",
            "created_at",
            "started_at",
            "done_at",
        )



class OrderVoidSer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default="")
    free_table = serializers.BooleanField(required=False)  # default True server-side

from django.db.models import Count  # keep this import near the top if not present

class OrderListSer(serializers.ModelSerializer):
    table_name = serializers.CharField(source="table.name", read_only=True)
    items_count = serializers.IntegerField(read_only=True)
    paid_by_name = serializers.CharField(source="paid_by.username", read_only=True)

    # NEW: quick display of who settled
    paid_by_name = serializers.CharField(source="paid_by.username", read_only=True)

    class Meta:
        model = Order
        fields = (
            "id", "number", "status", "payment_method",
            "subtotal", "tax", "total",
            "created_at", "paid_at",
            "table_name", "customer_name",
            "tab_opened_at", "tab_closed_at",
            "items_count",
            "paid_by",
            "paid_by_name",
        )

    def get_table_name(self, obj):
        return (
            getattr(obj.table, "name", None)
            or getattr(obj, "table_name_snapshot", "")
            or "-"
        )


# orders/serializers.py

class TableAdminInSer(serializers.ModelSerializer):
    class Meta:
        model = Table
        fields = ("name", "sort", "status")   # allow setting status=closed from UI
        extra_kwargs = {
            "name": {"required": True, "max_length": 30},
            "sort": {"required": False},
            "status": {"required": False},
        }

    def validate_name(self, v):
        v = (v or "").strip()
        if not v:
            raise serializers.ValidationError("Name is required.")
        return v



from rest_framework import serializers
from .models import CashSession, Expense

class CashSessionSer(serializers.ModelSerializer):
    sales_actual = serializers.SerializerMethodField()
    sales_tab_today = serializers.SerializerMethodField()
    sales_tab_previous = serializers.SerializerMethodField()
    expenses_total = serializers.SerializerMethodField()
    expected_cash = serializers.SerializerMethodField()
    over_short = serializers.SerializerMethodField()

    class Meta:
        model = CashSession
        fields = [
            "id", "opened_at", "opened_by", "starting_balance", "note",
            "closed_at", "closed_by", "counted_cash",
            "sales_actual", "sales_tab_today", "sales_tab_previous",
            "expenses_total", "expected_cash", "over_short",
        ]
        read_only_fields = ["opened_at", "closed_at", "opened_by", "closed_by",
                            "sales_actual", "sales_tab_today", "sales_tab_previous",
                            "expenses_total", "expected_cash", "over_short"]

    def get_sales_actual(self, obj): return f"{obj.sales_actual():.2f}"
    def get_sales_tab_today(self, obj): return f"{obj.sales_tab_today():.2f}"
    def get_sales_tab_previous(self, obj): return f"{obj.sales_tab_previous():.2f}"
    def get_expenses_total(self, obj): return f"{obj.expenses_sum():.2f}"
    def get_expected_cash(self, obj): return f"{obj.expected_cash():.2f}"
    def get_over_short(self, obj):
        v = obj.over_short()
        return None if v is None else f"{v:.2f}"


class ExpenseSer(serializers.ModelSerializer):
    class Meta:
        model = Expense
        fields = ["id", "session", "amount", "category", "note", "created_at", "created_by"]
        read_only_fields = ["id", "created_at", "created_by"]

    def create(self, validated):
        user = self.context["request"].user if "request" in self.context else None
        validated["created_by"] = user
        return super().create(validated)



# orders/serializers.py
from rest_framework import serializers
from .models import Order, OrderItem
from decimal import Decimal

class BackfillOrderItemInSer(serializers.Serializer):
    product_name = serializers.CharField()
    variant_name = serializers.CharField(required=False, allow_blank=True)
    qty = serializers.IntegerField(min_value=1)
    unit_price = serializers.DecimalField(max_digits=10, decimal_places=2)
    notes = serializers.CharField(required=False, allow_blank=True)

class BackfillOrderInSer(serializers.Serializer):
    number = serializers.CharField(required=False, allow_blank=True) # optional
    created_at = serializers.DateTimeField()
    paid_at = serializers.DateTimeField()
    payment_method = serializers.ChoiceField(choices=["cash","card","qr","other"])
    table_name = serializers.CharField(required=False, allow_blank=True)
    items = BackfillOrderItemInSer(many=True)
    tax_rate = serializers.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    external_ref = serializers.CharField(required=False, allow_blank=True)
    silent = serializers.BooleanField(required=False, default=True)  # default: no sounds

    # orders/serializers.py  (BackfillOrderInSer.create)

    def create(self, data):
        from django.utils import timezone
        from .models import Order, OrderItem
        from decimal import Decimal

        items    = data.pop("items", [])
        tax_rate = data.pop("tax_rate")
        silent   = data.pop("silent", True)
        ext      = data.pop("external_ref", "")
        num      = data.pop("number", "") or None
        table_nm = data.get("table_name") or ""

        pm = (data["payment_method"] or "cash").lower()
        if pm in {"bank", "transfer"}:
            pm = "transfer"
        elif pm == "qr":
            pm = "qr"

        # ⬇️ ONLY include fields that are guaranteed to exist
        order = Order.objects.create(
            number=num,
            status="paid",
            payment_method=pm,
            created_at=data["created_at"],
            paid_at=data["paid_at"],
            table=None,
        )

        # ⬇️ Set optional fields only if the Order model has them
        if hasattr(order, "table_name_snapshot"):
            order.table_name_snapshot = table_nm
        if hasattr(order, "source"):
            order.source = "backfill"
        if hasattr(order, "external_ref"):
            order.external_ref = ext

        subtotal = Decimal("0.00")
        for it in items:
            lt = Decimal(it["unit_price"]) * it["qty"]
            subtotal += lt
            OrderItem.objects.create(
                order=order,
                qty=it["qty"],
                unit_price=it["unit_price"],
                line_total=lt,
                product_name_snapshot=it["product_name"],
                variant_name_snapshot=it.get("variant_name", ""),
                notes=it.get("notes", ""),
            )

        order.subtotal = subtotal
        order.tax = (subtotal * tax_rate / Decimal("100")).quantize(Decimal("0.01"))
        order.total = (order.subtotal + order.tax).quantize(Decimal("0.01"))

        if hasattr(order, "backfilled_at"):
            order.backfilled_at = timezone.now()
        if hasattr(order, "backfilled_by"):
            req = self.context.get("request")
            order.backfilled_by = getattr(req, "user", None) if req else None

        order.save(update_fields=["subtotal", "tax", "total",
                                *([ "table_name_snapshot" ] if hasattr(order,"table_name_snapshot") else []),
                                *([ "source" ] if hasattr(order,"source") else []),
                                *([ "external_ref" ] if hasattr(order,"external_ref") else []),
                                *([ "backfilled_at" ] if hasattr(order,"backfilled_at") else []),
                                *([ "backfilled_by" ] if hasattr(order,"backfilled_by") else []),
                                ])

        # optional broadcast if not silent...
        return order


# orders/serializers.py
from .models import Withdraw

class WithdrawSer(serializers.ModelSerializer):
    class Meta:
        model = Withdraw
        fields = "__all__"
