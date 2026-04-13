# orders/models.py
from django.db import models, transaction
from django.utils import timezone
from django.core.validators import MinValueValidator
from decimal import Decimal
from datetime import datetime, time
from django.utils import timezone
import random
from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from django.db.models import Sum
from decimal import Decimal
from catalog.models import Product, ProductVariant, ModifierOption




class OrderDayCounter(models.Model):
    day = models.DateField(unique=True)
    seq = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.day} -> {self.seq}"
    
def gen_order_number() -> str:
    """
    YYMMDD + daily sequence starting at 1 (e.g., 2510021, 2510022, ...).
    Race-safe using SELECT ... FOR UPDATE.
    """
    today = timezone.localdate()
    yymmdd = today.strftime("%y%m%d")
    with transaction.atomic():
        counter, _ = OrderDayCounter.objects.select_for_update().get_or_create(day=today)
        counter.seq += 1
        counter.save(update_fields=["seq"])
        return f"{yymmdd}{counter.seq}"


class Table(models.Model):
    STATUS = (("free", "Free"), ("occupied", "Occupied"), ("closed", "Closed"))

    name = models.CharField(max_length=30, unique=True)
    status = models.CharField(max_length=10, choices=STATUS, default="free")
    sort = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.name} ({self.status})"

    def free(self, save: bool = True):
        self.status = "free"
        if save:
            self.save(update_fields=["status"])

    def occupy(self, save: bool = True):
        self.status = "occupied"
        if save:
            self.save(update_fields=["status"])


from django.db import models, transaction
from django.utils import timezone
from django.conf import settings
from django.db.models import Sum
from decimal import Decimal

# orders/models.py
from django.db import models, transaction
from django.utils import timezone
from django.core.validators import MinValueValidator
from django.conf import settings
from django.db.models import Sum
from decimal import Decimal

# ... your other imports (Product, etc.) and helpers like gen_order_number ...

from typing import Optional
class Order(models.Model):
    STATUS = (("open", "Open"), ("tab", "tab"), ("paid", "Paid"), ("void", "Void"))
    PAYMENT_METHODS = [
        ("cash", "Cash"),
        ("card", "Card"),
        ("qr", "Thai QR"),
        ("transfer", "Bank Transfer"),
        ("other", "Other"),
        ("pending", "Pending / Pay Later"),  # 👈 add this line
    ]

    number = models.CharField(max_length=20, unique=True, default=gen_order_number)

    table = models.ForeignKey(
        Table, null=True, blank=True, on_delete=models.SET_NULL, related_name="orders"
    )
    # ✅ NEW: snapshot for printing when FK is null (e.g., backfill, takeaway, archived table)
    table_name_snapshot = models.CharField(max_length=50, blank=True, default="")

    status = models.CharField(max_length=12, choices=STATUS, default="open")

    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    payment_method = models.CharField(
        max_length=16,
        choices=PAYMENT_METHODS,
        default="cash"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    void_reason = models.CharField(max_length=200, blank=True, default="")
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="voided_orders"
    )
    voided_at = models.DateTimeField(null=True, blank=True)

    customer_name = models.CharField(max_length=120, blank=True, default="")
    credit_remark = models.TextField(blank=True, default="")
    credit_given = models.BooleanField(default=False)
    tab_opened_at = models.DateTimeField(null=True, blank=True)
    tab_closed_at = models.DateTimeField(null=True, blank=True)

    # ---- NEW: audit fields used by backfill serializer ----
    source = models.CharField(max_length=32, blank=True, default="")        # e.g., "backfill", "pos"
    external_ref = models.CharField(max_length=64, blank=True, default="")  # ledger/slip no.
    backfilled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+"
    )
    backfilled_at = models.DateTimeField(null=True, blank=True)

    # ---- track who did actions ----
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="paid_orders"
    )
    tab_opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="tabs_opened"
    )
    tab_closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="tabs_closed"
    )

    class Meta:
        ordering = ("-id",)
        indexes = [
            models.Index(fields=["number"]),
            models.Index(fields=["status"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["paid_at"]),
            models.Index(fields=["paid_by"]),
            models.Index(fields=["tab_opened_by"]),
            models.Index(fields=["tab_closed_by"]),
            models.Index(fields=["external_ref"]),   # ✅ helpful for backfill lookups
        ]

    def __str__(self):
        return self.number

    def recompute_totals(self, tax_rate: Optional[Decimal] = None, save: bool = True):
        gross = (
            OrderItem.objects
            .filter(order_id=self.id)
            .aggregate(s=Sum("line_total"))
            .get("s") or Decimal("0.00")
        )
        comp_total = (
            OrderComp.objects
            .filter(order_id=self.id, voided_at__isnull=True)
            .aggregate(s=Sum("amount"))
            .get("s") or Decimal("0.00")
        )

        net_subtotal = gross - comp_total
        if net_subtotal < 0:
            net_subtotal = Decimal("0.00")

        if tax_rate is None:
            if (self.subtotal or 0) > 0 and self.tax is not None:
                tax_rate = (Decimal(self.tax) / Decimal(self.subtotal)) * Decimal("100.00")
            else:
                tax_rate = Decimal("0.00")

        tax = (net_subtotal * Decimal(tax_rate) / Decimal("100.00")).quantize(Decimal("0.01"))
        self.subtotal = net_subtotal
        self.tax = tax
        self.total = net_subtotal + tax

        if save:
            Order.objects.filter(pk=self.pk).update(subtotal=self.subtotal, tax=self.tax, total=self.total)

    @property
    def gross_subtotal(self):
        return (
            OrderItem.objects
            .filter(order_id=self.id)
            .aggregate(s=Sum("line_total"))
            .get("s") or Decimal("0.00")
        )

    @property
    def comps_total(self):
        return (
            OrderComp.objects
            .filter(order_id=self.id, voided_at__isnull=True)
            .aggregate(s=Sum("amount"))
            .get("s") or Decimal("0.00")
        )

    @transaction.atomic
    def open_tab(self, customer_name: str = "", remark: str = "", credit_given: Optional[bool] = None, opened_by=None):
        if self.status in ("paid", "void"):
            return
        self.status = "tab"
        # A tab is an order state, not a real payment method.
        # Keep unpaid tabs as pending until they are settled.
        self.payment_method = "pending"
        if customer_name:
            self.customer_name = customer_name
        if remark:
            self.credit_remark = remark
        if credit_given is not None:
            self.credit_given = bool(credit_given)
        if not self.tab_opened_at:
            self.tab_opened_at = timezone.now()
        if opened_by is not None and not self.tab_opened_by_id:
            self.tab_opened_by = opened_by

        self.save(update_fields=[
            "status", "payment_method", "customer_name",
            "credit_remark", "credit_given", "tab_opened_at", "tab_opened_by",
        ])

        if self.credit_given and self.table_id:
            try:
                if self.table.name.lower() != "takeaway" and self.table.status != "free":
                    self.table.status = "free"
                    self.table.save(update_fields=["status"])
            except Exception:
                pass

    @transaction.atomic
    def settle(self, payment_method: str = "cash", free_table: bool = False, paid_by=None):
        if self.paid_at:
            return
        if self.status == "tab" and not self.tab_closed_at:
            self.tab_closed_at = timezone.now()

        self.payment_method = (payment_method or "cash").lower()
        self.status = "paid"
        self.paid_at = timezone.now()

        if paid_by is not None:
            self.paid_by = paid_by
            if not self.tab_closed_by_id:
                self.tab_closed_by = paid_by

        self.save(update_fields=[
            "payment_method", "status", "paid_at", "tab_closed_at",
            "paid_by", "tab_closed_by",
        ])

    def save(self, *args, **kwargs):
        if not self.number:
            while True:
                candidate = gen_order_number()
                if not Order.objects.filter(number=candidate).exists():
                    self.number = candidate
                    break
        super().save(*args, **kwargs)


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    base_price_snapshot = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    variant_price_snapshot = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="order_items",
    )


    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.SET_NULL,   # ✅ allow safe deletion/archival
        null=True,
        blank=True,
        related_name="order_items",
    )
    qty = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])

    # 🔒 price snapshot (already present)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)  # computed at add
    line_total = models.DecimalField(max_digits=10, decimal_places=2)

    # 🔒 NEW: immutable name snapshots (set once at creation)
    product_name_snapshot = models.CharField(max_length=150, default="")   # 👈 new
    variant_name_snapshot = models.CharField(max_length=50,  default="")   # 👈 new

    notes = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ("id",)

    def __str__(self):
        # Use snapshots so old receipts always show what the customer saw
        v = f" ({self.variant_name_snapshot})" if self.variant_name_snapshot else ""
        return f"{self.qty} × {self.product_name_snapshot}{v}"

    def recompute(self, save: bool = True):
        unit = self.unit_price if isinstance(self.unit_price, Decimal) else Decimal(str(self.unit_price or 0))
        qty = Decimal(self.qty or 0)
        self.line_total = unit * qty
        if save:
            self.save(update_fields=["line_total"])
        return self.line_total

class OrderItemModifier(models.Model):
    order_item = models.ForeignKey(OrderItem, on_delete=models.CASCADE, related_name="modifiers")
    option = models.ForeignKey(ModifierOption, on_delete=models.SET_NULL, null=True, blank=True, related_name="order_item_modifiers")
    include = models.BooleanField(default=True)
    price_delta = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    qty = models.PositiveIntegerField(default=1)  # ✅ new field

    class Meta:
        ordering = ("id",)

    def __str__(self):
        name = (self.option.name if self.option else "—")
        prefix = "" if self.include else "No "
        return f"{prefix}{name}"


class KitchenTicket(models.Model):
    STATUS = (("queued", "Queued"), ("in_progress", "In Progress"), ("done", "Done"), ("cancelled", "Cancelled"),)

    order = models.ForeignKey(Order, related_name="tickets", on_delete=models.CASCADE)
    # item = models.OneToOneField(OrderItem, related_name="kitchen_ticket", on_delete=models.CASCADE)
    item = models.ForeignKey(OrderItem, on_delete=models.CASCADE, related_name="tickets")

    station = models.CharField(max_length=50, default="MAIN")
    status = models.CharField(max_length=12, choices=STATUS, default="queued")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    done_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["station", "status"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"KT#{self.pk} {self.station} [{self.status}] for {self.order.number}"

from django.db import models, transaction
from django.utils import timezone
from django.core.validators import MinValueValidator
from django.conf import settings
from django.db.models import Sum, Q, CheckConstraint, F
from decimal import Decimal

class OrderComp(models.Model):
    SCOPE_CHOICES = [("order", "Order"), ("item", "Item")]
    MODE_CHOICES = [("qty", "By Quantity"), ("amount", "By Amount"), ("percent", "By Percent")]

    order = models.ForeignKey("orders.Order", on_delete=models.CASCADE, related_name="comps")
    item  = models.ForeignKey("orders.OrderItem", on_delete=models.CASCADE,
                              related_name="comps", null=True, blank=True)
    scope = models.CharField(max_length=10, choices=SCOPE_CHOICES)
    mode  = models.CharField(max_length=10, choices=MODE_CHOICES)

    qty   = models.IntegerField(null=True, blank=True)
    percent = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    unit_price_snapshot = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    reason = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                  related_name="voided_comps", on_delete=models.SET_NULL)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["order", "voided_at"]),
            models.Index(fields=["item"]),
            models.Index(fields=["created_at"]),
        ]
        constraints = [
            # scope='item' => item is not null; scope='order' => item is null
            CheckConstraint(
                name="comp_item_required_for_item_scope",
                check=Q(scope="item", item__isnull=False) | Q(scope="order", item__isnull=True),
            ),
        ]

    @property
    def active(self):
        return self.voided_at is None

    def __str__(self):
        tgt = f"Item#{self.item_id}" if self.item_id else "Order"
        return f"Comp {self.mode} {tgt} amount={self.amount} ({'active' if self.active else 'void'})"


# orders/models.py (and the Expense/CashSession models)
from django.db import models

class AuditMixin(models.Model):
    source = models.CharField(max_length=32, default="", blank=True)        # "backfill", "pos", "import", etc.
    external_ref = models.CharField(max_length=64, default="", blank=True)  # ledger id / slip no.
    backfilled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+"
    )
    backfilled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True
        indexes = [models.Index(fields=["external_ref"])]


# orders/models.py (append at bottom; keep your existing imports)
from django.conf import settings
from django.db.models import Sum, Q
from django.utils import timezone
from datetime import datetime, time
from decimal import Decimal

class CashSession(AuditMixin, models.Model):
    # Was: opened_at = models.DateTimeField(auto_now_add=True)
    opened_at = models.DateTimeField(default=timezone.now)          # ✅ can override from UI
    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="cashsessions_opened"
    )
    starting_balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    note = models.CharField(max_length=255, blank=True, default="")
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="cashsessions_closed"
    )
    counted_cash = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        status = "OPEN" if not self.closed_at else "CLOSED"
        return f"Session #{self.pk} {status} @ {self.opened_at.astimezone().strftime('%Y-%m-%d %H:%M')}"

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    def _time_window(self):
        start = self.opened_at
        end = self.closed_at or timezone.now()
        return (start, end)

    def _reporting_day_window(self):
        """
        Classify "tab today" vs "previous tabs" relative to the reporting day:
        - open session: today in store timezone
        - closed session: the day the session was closed
        Always intersect with the session window so old sessions don't pick up future data.
        """
        ref = self.closed_at or timezone.now()
        day = timezone.localdate(ref)
        start = timezone.make_aware(datetime.combine(day, time.min))
        end = timezone.make_aware(datetime.combine(day, time.max))
        return start, end

    def _cash_paid_orders_qs(self):
        start, end = self._time_window()
        return Order.objects.filter(
            status="paid",
            payment_method="cash",
            paid_at__gte=start,
            paid_at__lte=end,
        )

    def sales_actual(self):
        """
        Only walk-in sales (no tabs at all).
        """
        start, end = self._time_window()

        s = Order.objects.filter(
            status="paid",
            payment_method="cash",
            paid_at__gte=start,
            paid_at__lte=end,
            tab_opened_at__isnull=True,   # 🔥 exclude ALL tabs
        ).aggregate(s=Sum("total"))["s"]

        return s or Decimal("0.00")



    def sales_tab_today(self):
        """
        Tabs opened on the reporting day within this session window.
        This is a tracking metric, not drawer cash.
        """
        day_start, day_end = self._reporting_day_window()
        session_start, session_end = self._time_window()

        s = Order.objects.filter(
            tab_opened_at__gte=max(day_start, session_start),
            tab_opened_at__lte=min(day_end, session_end),
            status__in=["tab", "paid"],
        ).aggregate(s=Sum("total"))["s"]

        return s or Decimal("0.00")

    def cash_from_tabs_opened_today(self):
        """
        Cash actually collected during this session for tabs opened on the reporting day.
        """
        day_start, day_end = self._reporting_day_window()
        session_start, session_end = self._time_window()

        s = Order.objects.filter(
            status="paid",
            payment_method="cash",
            tab_opened_at__gte=max(day_start, session_start),
            tab_opened_at__lte=min(day_end, session_end),
            paid_at__gte=session_start,
            paid_at__lte=session_end,
        ).aggregate(s=Sum("total"))["s"]

        return s or Decimal("0.00")

    def sales_tab_previous(self):
        """
        Cash collected during this session for tabs opened before the reporting day.
        """
        session_start, session_end = self._time_window()
        day_start, _ = self._reporting_day_window()

        s = Order.objects.filter(
            status="paid",
            payment_method="cash",
            tab_opened_at__lt=day_start,
            paid_at__gte=session_start,
            paid_at__lte=session_end,
        ).aggregate(s=Sum("total"))["s"]

        return s or Decimal("0.00")



    def expenses(self) -> Decimal:
        """
        Sum of expenses tied to this session (and within its time window as a safety).
        """
        start, end = self._time_window()
        s = Expense.objects.filter(
            session=self,
            created_at__gte=start,
            created_at__lte=end,
        ).aggregate(s=Sum("amount"))["s"]
        return s or Decimal("0.00")

    def expenses_sum(self) -> Decimal:
        """
        Sum of expenses tied to this session (and within its time window as a safety).
        """
        start, end = self._time_window()
        s = Expense.objects.filter(
            session=self,
            created_at__gte=start,
            created_at__lte=end,
        ).aggregate(s=Sum("amount"))["s"]
        return s or Decimal("0.00")

    def expected_cash(self) -> Decimal:
        withdraw_sum = (
            Withdraw.objects.filter(session=self).aggregate(s=Sum("amount"))["s"]
            or Decimal("0.00")
        )
        return (
            (self.starting_balance or Decimal("0.00"))
            + self.sales_actual()
            + self.cash_from_tabs_opened_today()
            + self.sales_tab_previous()
            - self.expenses_sum()
            - withdraw_sum
        )


    def over_short(self) -> Optional[Decimal]:
        """
        Counted - Expected; positive=over, negative=short. Only available after close.
        """
        if self.counted_cash is None:
            return None
        return (self.counted_cash or Decimal("0.00")) - self.expected_cash()


class Expense(AuditMixin, models.Model):
    session = models.ForeignKey(CashSession, on_delete=models.SET_NULL, null=True, blank=True, related_name="expenses")
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    category = models.CharField(max_length=80, blank=True, default="")
    note = models.CharField(max_length=255, blank=True, default="")
    # Was: created_at = models.DateTimeField(auto_now_add=True)
    created_at = models.DateTimeField(default=timezone.now)         # ✅ can override from UI
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return f"Expense {self.amount} ({self.category or 'uncategorized'})"



# orders/models.py
class Withdraw(AuditMixin, models.Model):
    session = models.ForeignKey(CashSession, on_delete=models.SET_NULL, null=True, blank=True, related_name="withdraws")
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    note = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return f"Withdraw {self.amount}"
