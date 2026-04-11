from django.contrib import admin
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from catalog.views import CategoryViewSet, ProductViewSet
from orders.views import OrderViewSet, KitchenTicketViewSet, TableViewSet, report_daily, kitchen_stations, report_range
from orders.auth_views import login, logout, me
from orders.views import TableViewSet
from catalog.views_admin import ProductAdminViewSet, ModifierGroupAdminViewSet, CategoryAdminViewSet  # <-- add this
from orders.views_cash import CashSessionViewSet, ExpenseViewSet, cash_daily_report, cash_daily_orders, cash_daily_expenses, WithdrawViewSet
from orders.views_tabs import tabs_paid
from orders.views_cash import backfill_expense, backfill_session
from orders.views_backfill import backfill_order
from orders.views_cash import cash_daily_tabs



router = DefaultRouter()
router.register("categories", CategoryViewSet, basename="category")
router.register("products", ProductViewSet, basename="product")
router.register("orders", OrderViewSet, basename="order")
router.register("kitchen-tickets", KitchenTicketViewSet, basename="kitchen-ticket")
router.register("tables", TableViewSet, basename="table")

router.register("admin/products", ProductAdminViewSet, basename="admin-products")  # <-- add this
router.register(r'admin/modifier-groups', ModifierGroupAdminViewSet, basename='admin-modifier-groups')
router.register("admin/categories", CategoryAdminViewSet, basename="admin-categories")

router.register("cash-sessions", CashSessionViewSet, basename="cash-session")
router.register("expenses", ExpenseViewSet, basename="expense")
router.register(r'withdraws', WithdrawViewSet)



urlpatterns = [
    path("admin/", admin.site.urls), 
    path("api/", include(router.urls)), 
    path("api/reports/daily/", report_daily, name="report-daily"),
    path("api/report/cash-daily/", cash_daily_report, name="cash-daily-report"),


    path("api/report/cash-daily/tabs/", cash_daily_tabs, name="cash-daily-tabs"),


    path("api/report/cash-daily/orders/", cash_daily_orders, name="cash-daily-orders"),       
    path("api/report/cash-daily/expenses/", cash_daily_expenses, name="cash-daily-expenses"), 

    path("api/backfill/orders/", backfill_order),
    path("api/backfill/expenses/", backfill_expense),
    path("api/backfill/sessions/", backfill_session),

    path("api/reports/range/", report_range, name="report-range"),
    path("api/kitchen-stations/", kitchen_stations, name="kitchen-stations"),
    path("api/orders/tabs/paid/", tabs_paid, name="tabs-paid"),
    # path("api/tabs/open/", tabs_open, name="tabs-open"),
    path("api/me/", me),
]

urlpatterns += [
    path("api/auth/login", login),
    path("api/auth/logout", logout),
]

from django.conf import settings
from django.conf.urls.static import static
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)