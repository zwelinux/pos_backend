from django.contrib import admin
from .models import Order, OrderItem, OrderItemModifier, KitchenTicket, Withdraw
admin.site.register([OrderItem, OrderItemModifier, KitchenTicket])
from .models import Table  # add
admin.site.register(Table)  # add

from django.contrib import admin
from .models import Order

@admin.action(description="Mark selected as PAID (Cash)")
def mark_paid_cash(modeladmin, request, queryset):
    for o in queryset.filter(status="tab", paid_at__isnull=True):
        o.settle(payment_method="cash", free_table=False)

@admin.action(description="Mark selected as PAID (Card)")
def mark_paid_card(modeladmin, request, queryset):
    for o in queryset.filter(status="tab", paid_at__isnull=True):
        o.settle(payment_method="card", free_table=False)

@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("number", "status", "customer_name", "total", "paid_at")
    list_filter = ("status", "payment_method")
    search_fields = ("number", "customer_name")
    actions = [mark_paid_cash, mark_paid_card]


from django.contrib import admin
from .models import CashSession, Expense

@admin.register(CashSession)
class CashSessionAdmin(admin.ModelAdmin):
    list_display = ("id", "opened_at", "opened_by", "starting_balance", "closed_at", "closed_by", "counted_cash")
    list_filter = ("opened_by", "closed_by")
    readonly_fields = ("opened_at", "closed_at")
    search_fields = ("id", "note")

@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ("id", "session", "amount", "category", "created_at", "created_by", "note")
    list_filter = ("category", "created_by")
    search_fields = ("note", "category")


admin.site.register(Withdraw)