# orders/views_cash.py
from datetime import datetime, time

from decimal import Decimal
from django.utils import timezone
from django.db.models import Sum
from rest_framework import status, viewsets
from rest_framework.decorators import api_view, permission_classes, action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.db.models import Q



from .models import CashSession, Expense, Withdraw, Order
from .serializers import CashSessionSer, ExpenseSer, WithdrawSer
from .permissions import InGroups
from rest_framework.exceptions import ValidationError


# --------------------------------------------------
# Helper
# --------------------------------------------------
def _day_range(day):
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(day, time.min), tz)
    end = timezone.make_aware(datetime.combine(day, time.max), tz)
    return start, end



# --------------------------------------------------
# Cash Sessions
# --------------------------------------------------
class CashSessionViewSet(viewsets.ModelViewSet):
    queryset = CashSession.objects.all()
    serializer_class = CashSessionSer
    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=["get"])
    def current(self, request):
        """Return current open cash session for this user."""
        ses = CashSession.objects.filter(
            opened_by=request.user,
            closed_at__isnull=True
        ).order_by("-id").first()

        if not ses:
            return Response({"active": False, "session": None})

        return Response({
            "active": True,
            "session": CashSessionSer(ses).data
        })


    def create(self, request, *args, **kwargs):
        """Open a new cash session."""
        if CashSession.objects.filter(closed_at__isnull=True, opened_by=request.user).exists():
            return Response({"detail": "You already have an open cash session."}, status=400)

        starting = Decimal(str(request.data.get("starting_balance", "0") or "0"))
        note = str(request.data.get("note") or "")

        ses = CashSession.objects.create(
            opened_by=request.user,
            starting_balance=starting,
            note=note,
        )
        return Response(
            {"detail": "Session opened successfully.", "session": CashSessionSer(ses).data},
            status=201,
        )
    

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        """
        Close an open cash session.
        POST /api/cash-sessions/{id}/close/
        Body: { "counted_cash": "1234.00" }
        """
        ses = self.get_object()

        if ses.closed_at:
            return Response(
                {"detail": "Session already closed."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        counted_cash = request.data.get("counted_cash")
        if counted_cash in ("", None):
            return Response(
                {"detail": "counted_cash is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            counted_cash = Decimal(str(counted_cash))
        except Exception:
            return Response(
                {"detail": "Invalid counted_cash value."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ses.counted_cash = counted_cash
        ses.closed_at = timezone.now()
        ses.closed_by = request.user
        ses.save(update_fields=["counted_cash", "closed_at", "closed_by"])

        return Response(
            {
                "detail": "Session closed successfully.",
                "session": CashSessionSer(ses).data,
            },
            status=status.HTTP_200_OK,
        )

    def _window(self, ses):
        start = ses.opened_at
        end = ses.closed_at or timezone.now()
        return start, end


    # @action(detail=True, methods=["get"])
    # def orders(self, request, pk=None):
    #     ses = self.get_object()
    #     start, end = self._window(ses)
    #     session_day = ses.opened_at.date()

    #     # 1. WALK-IN ACTUAL SALES
    #     actual_qs = Order.objects.filter(
    #         status="paid",
    #         payment_method="cash",
    #         paid_at__gte=start,
    #         paid_at__lte=end,
    #         tab_opened_at__isnull=True,
    #     )

    #     # 2. TAB SALES OPENED TODAY (paid + unpaid)
    #     tab_today_qs = Order.objects.filter(
    #         tab_opened_at__date=session_day,
    #         status__in=["tab", "paid"],
    #     )

    #     # 3. PREVIOUS TABS PAID TODAY
    #     session_day_start = timezone.make_aware(datetime.combine(session_day, t(0, 0, 0)))

    #     prev_tab_qs = Order.objects.filter(
    #         status="paid",
    #         payment_method="cash",
    #         paid_at__gte=start,
    #         paid_at__lte=end,
    #         tab_opened_at__lt=session_day_start,
    #     )

    #     def serialize(qs, category):
    #         return [{
    #             "id": o.id,
    #             "number": o.number,
    #             "paid_at": o.paid_at,
    #             "total": o.total,
    #             "customer_name": o.customer_name,
    #             "tab_opened_at": o.tab_opened_at,
    #             "sale_category": category,
    #         } for o in qs]

    #     return Response({
    #         "actual": serialize(actual_qs, "actual"),
    #         "tab_today": serialize(tab_today_qs, "tab_today"),
    #         "previous_tab": serialize(prev_tab_qs, "previous_tab"),
    #     })


    @action(detail=True, methods=["get"])
    def orders(self, request, pk=None):
        ses = self.get_object()
        start, end = self._window(ses)

        session_day = ses.opened_at.date()
        day_start = timezone.make_aware(datetime.combine(session_day, time.min))
        day_end = timezone.make_aware(datetime.combine(session_day, time.max))

        # 1. ACTUAL (Walk-In)
        actual_qs = Order.objects.filter(
            status="paid",
            payment_method="cash",
            paid_at__gte=start,
            paid_at__lte=end,
            tab_opened_at__isnull=True,
        ).order_by("-paid_at")

        # 2. TAB TODAY
        tab_today_qs = Order.objects.filter(
            tab_opened_at__date=session_day,
            status__in=["tab", "paid"],
            tab_opened_at__gte=start,
            tab_opened_at__lte=end,
        ).order_by("-tab_opened_at")

        # 3. PREVIOUS TAB PAID DURING THIS SESSION
        previous_tab_qs = Order.objects.filter(
            status="paid",
            payment_method="cash",
            paid_at__gte=start,
            paid_at__lte=end,
            tab_opened_at__lt=day_start,
        ).order_by("-paid_at")

        def serialize(qs, category):
            return [
                {
                    "id": o.id,
                    "number": o.number,
                    "paid_at": o.paid_at,
                    "total": o.total,
                    "customer_name": o.customer_name,
                    "tab_opened_at": o.tab_opened_at,
                    "sale_category": category,
                }
                for o in qs
            ]

        return Response({
            "actual": serialize(actual_qs, "actual"),
            "tab_today": serialize(tab_today_qs, "tab_today"),
            "previous_tab": serialize(previous_tab_qs, "previous_tab"),
        })



    @action(detail=True, methods=["get"])
    def expenses(self, request, pk=None):
        ses = self.get_object()
        start, end = self._window(ses)

        qs = Expense.objects.filter(
            session=ses,
            created_at__gte=start,
            created_at__lte=end,
        ).order_by("-created_at")

        return Response(ExpenseSer(qs, many=True).data)
        
    @action(detail=True, methods=["get"])
    def tab_today(self, request, pk=None):
        ses = self.get_object()
        session_day = ses.opened_at.date()

        qs = Order.objects.filter(
            tab_opened_at__date=session_day,
            status__in=["tab", "paid"],
        ).order_by("-paid_at", "-tab_opened_at")

        data = [{
            "id": o.id,
            "number": o.number,
            "paid_at": o.paid_at,
            "total": o.total,
            "customer_name": o.customer_name,
            "tab_opened_at": o.tab_opened_at,
        } for o in qs]

        return Response(data)


# --------------------------------------------------
# Expenses
# --------------------------------------------------
class ExpenseViewSet(viewsets.ModelViewSet):
    """
    /api/expenses/ [GET, POST]
    """
    queryset = Expense.objects.select_related("session").all()
    serializer_class = ExpenseSer
    permission_classes = [IsAuthenticated]

    def perform_create(self, serializer):
        session = serializer.validated_data.get("session")

        if session is None:
            session = CashSession.objects.filter(
                closed_at__isnull=True,
                opened_by=self.request.user
            ).order_by("-id").first()
            if not session:
                raise ValidationError({"detail": "No open cash session for this user."})
            serializer.validated_data["session"] = session

        amount = serializer.validated_data["amount"]

        # 🔒 Check available balance before spending
        available = session.expected_cash()
        if amount > available:
            raise ValidationError({"detail": f"Insufficient balance. Available: ฿{available:.2f}"})

        serializer.save(created_by=self.request.user)



# orders/views_cash.py
from .models import CashSession, Expense, Withdraw, Order
from .serializers import CashSessionSer, ExpenseSer  # add WithdrawSer below


class WithdrawViewSet(viewsets.ModelViewSet):
    """
    /api/withdraws/ [GET, POST]
    """
    queryset = Withdraw.objects.select_related("session").all()
    serializer_class = WithdrawSer
    permission_classes = [IsAuthenticated]

    def perform_create(self, serializer):
        session = serializer.validated_data.get("session")

        if session is None:
            session = CashSession.objects.filter(
                closed_at__isnull=True,
                opened_by=self.request.user
            ).order_by("-id").first()
            if not session:
                raise ValidationError({"detail": "No open cash session for this user."})
            serializer.validated_data["session"] = session

        amount = serializer.validated_data["amount"]

        # 🔒 Check available balance before withdrawal
        available = session.expected_cash()
        if amount > available:
            raise ValidationError({"detail": f"Insufficient balance. Available: ฿{available:.2f}"})

        serializer.save(created_by=self.request.user)


# --------------------------------------------------
# Reports
# --------------------------------------------------
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def cash_daily_report(request):
    """
    Daily cash balance report (6-line summary).
    Query params:
      - date=YYYY-MM-DD (optional)
      - session_id (optional) to report for that session only
    """
    date_str = request.query_params.get("date")
    session_id = request.query_params.get("session_id")

    # --- Session mode ---
    if session_id:
        try:
            ses = CashSession.objects.get(pk=session_id)
        except CashSession.DoesNotExist:
            return Response({"detail": "Session not found."}, status=404)
        
        withdraw_money = (
            Withdraw.objects.filter(session=ses).aggregate(s=Sum("amount"))["s"]
            or Decimal("0.00")
        )


        data = {
            "scope": f"session:{ses.id}",
            "starting_balance": f"{ses.starting_balance:.2f}",
            "sales_money_actual": f"{ses.sales_actual():.2f}",
            "sales_money_tab_today": f"{ses.sales_tab_today():.2f}",
            "sales_money_from_previous_tab": f"{ses.sales_tab_previous():.2f}",
            "expense_money": f"{ses.expenses_sum():.2f}",
            "withdraw_money": f"{withdraw_money:.2f}",
            "total_money": f"{ses.expected_cash():.2f}",
            "closed": bool(ses.closed_at),
            "counted_cash": (
                None if ses.counted_cash is None else f"{ses.counted_cash:.2f}"
            ),
            "over_short": (
                None if ses.over_short() is None else f"{ses.over_short():.2f}"
            ),
        }
        return Response(data, status=200)

    # --- Daily mode ---
    day = timezone.localdate()
    if date_str:
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return Response({"detail": "Invalid date format."}, status=400)

    start, end = _day_range(day)
    first_ses = (
        CashSession.objects
        .filter(opened_at__date=day)
        .order_by("opened_at")
        .first()
    )
    starting_balance = first_ses.starting_balance if first_ses else Decimal("0.00")

    paid_cash = Order.objects.filter(
        status="paid", payment_method="cash", paid_at__gte=start, paid_at__lte=end
    )

    sales_money_actual = (
        paid_cash.filter(tab_opened_at__isnull=True).aggregate(s=Sum("total"))["s"]
        or Decimal("0.00")
    )
    # sales_money_tab_today = (
    #     paid_cash.filter(tab_opened_at__isnull=False, tab_opened_at__date=day)
    #     .aggregate(s=Sum("total"))["s"]
    #     or Decimal("0.00")
    # )

    sales_money_tab_today = (
        Order.objects.filter(
            tab_opened_at__date=day,
            status__in=["tab", "paid"],
        ).aggregate(s=Sum("total"))["s"]
        or Decimal("0.00")
    )



    # sales_money_from_previous_tab = (
    #     paid_cash.filter(tab_opened_at__isnull=False, tab_opened_at__date__lt=day)
    #     .aggregate(s=Sum("total"))["s"]
    #     or Decimal("0.00")
    # )

    sales_money_from_previous_tab = (
        paid_cash.filter(tab_opened_at__isnull=False, tab_opened_at__date__lt=day)
        .aggregate(s=Sum("total"))["s"]
        or Decimal("0.00")
    )

    expense_money = (
        Expense.objects.filter(created_at__gte=start, created_at__lte=end).aggregate(
            s=Sum("amount")
        )["s"]
        or Decimal("0.00")
    )

    withdraw_money = (
        Withdraw.objects.filter(created_at__gte=start, created_at__lte=end)
        .aggregate(s=Sum("amount"))["s"]
        or Decimal("0.00")
    )

    total_money = (
        starting_balance
        + sales_money_actual
        + sales_money_tab_today
        + sales_money_from_previous_tab
        - expense_money
        - withdraw_money
    )

    data = {
        "scope": f"day:{day.isoformat()}",
        "starting_balance": f"{starting_balance:.2f}",
        "sales_money_actual": f"{sales_money_actual:.2f}",
        "sales_money_tab_today": f"{sales_money_tab_today:.2f}",
        "sales_money_from_previous_tab": f"{sales_money_from_previous_tab:.2f}",
        "expense_money": f"{expense_money:.2f}",
        "withdraw_money": f"{withdraw_money:.2f}",
        "total_money": f"{total_money:.2f}",
    }

    return Response(data, status=200)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def cash_daily_orders(request):
    """List of CASH orders paid on a given day."""
    date_str = request.query_params.get("date")
    day = timezone.localdate()
    if date_str:
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return Response({"detail": "Invalid date format."}, status=400)

    start, end = _day_range(day)
    qs = (
        Order.objects.filter(
            status="paid", payment_method="cash", paid_at__gte=start, paid_at__lte=end
        )
        .values("id", "number", "total", "customer_name", "paid_at", "tab_opened_at")
        .order_by("-paid_at")
    )
    return Response(list(qs), status=200)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def cash_daily_expenses(request):
    """List of expenses created on a given day."""
    date_str = request.query_params.get("date")
    day = timezone.localdate()
    if date_str:
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return Response({"detail": "Invalid date format."}, status=400)

    start, end = _day_range(day)
    qs = Expense.objects.filter(created_at__gte=start, created_at__lte=end).order_by(
        "-created_at"
    )
    return Response(ExpenseSer(qs, many=True).data, status=200)


# --------------------------------------------------
# Backfill
# --------------------------------------------------
@api_view(["POST"])
@permission_classes([IsAuthenticated, InGroups("Manager")])
def backfill_expense(request):
    """Create expense for past records."""
    amount = request.data.get("amount")
    category = request.data.get("category", "")
    note = request.data.get("note", "")
    created_at = request.data.get("created_at")  # ISO string
    external_ref = request.data.get("external_ref", "")

    if not amount:
        return Response({"detail": "amount required"}, status=400)

    e = Expense.objects.create(
        amount=Decimal(str(amount)),
        category=category,
        note=note,
        source="backfill",
        external_ref=external_ref,
        created_by=request.user,
    )

    if created_at:
        from django.utils import timezone as djtz
        try:
            dt = datetime.fromisoformat(created_at)
            if djtz.is_naive(dt):
                dt = djtz.make_aware(dt, djtz.get_current_timezone())
            e.created_at = dt
            e.save(update_fields=["created_at"])
        except Exception:
            pass

    return Response({"id": e.id}, status=201)


@api_view(["POST"])
@permission_classes([IsAuthenticated, InGroups("Manager")])
def backfill_session(request):
    """Create a session for past dates (manager only)."""
    from django.utils import timezone as djtz
    data = request.data

    ses = CashSession.objects.create(
        opened_by=request.user,
        starting_balance=Decimal(str(data.get("starting_balance", "0.00"))),
        note=data.get("note", ""),
        source="backfill",
        backfilled_by=request.user,
        backfilled_at=djtz.now(),
    )

    def _parse(ts):
        d = datetime.fromisoformat(ts)
        return djtz.make_aware(d, djtz.get_current_timezone()) if djtz.is_naive(d) else d

    if data.get("opened_at"):
        ses.opened_at = _parse(data["opened_at"])
    if data.get("closed_at"):
        ses.closed_at = _parse(data["closed_at"])
        ses.closed_by = request.user
    if data.get("counted_cash") is not None:
        ses.counted_cash = Decimal(str(data["counted_cash"]))
    ses.save()

    return Response({"id": ses.id}, status=201)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def cash_daily_tabs(request):
    """Return tab orders for a given day: both unpaid and paid."""
    date_str = request.query_params.get("date")
    day = timezone.localdate()
    if date_str:
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return Response({"detail": "Invalid date format."}, status=400)

    qs = Order.objects.filter(
        tab_opened_at__date=day,
        status__in=["tab", "paid"],
    ).order_by("-paid_at", "-tab_opened_at")

    data = [{
        "id": o.id,
        "number": o.number,
        "paid_at": o.paid_at,
        "total": o.total,
        "customer_name": o.customer_name,
        "tab_opened_at": o.tab_opened_at,
    } for o in qs]

    return Response(data, status=200)
