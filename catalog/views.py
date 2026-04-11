from rest_framework import viewsets
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.permissions import SAFE_METHODS
from .models import Category, Product
from .serializers import CategorySer, ProductListSer, ProductDetailSer
from rest_framework.decorators import action


class CategoryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Category.objects.filter(is_active=True)
    serializer_class = CategorySer
    permission_classes = [AllowAny]

from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db.models import Sum
from django.db.models.functions import TruncDate
from orders.models import OrderItem
from catalog.models import Product
from .serializers import ProductListSer, ProductDetailSer


class ProductViewSet(viewsets.ReadOnlyModelViewSet):
    def get_permissions(self):
        if self.request.method in SAFE_METHODS:
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_queryset(self):
        qs = (
            Product.objects.filter(is_active=True)
            .select_related("category")
            .prefetch_related(
                "variants",                          # for detail
                "modifier_links__group__options",    # groups + their options for detail
            )
            .order_by("id")
        )
        cat = self.request.query_params.get("category")
        return qs.filter(category_id=cat) if cat else qs

    def get_serializer_class(self):
        return ProductDetailSer if self.action == "retrieve" else ProductListSer

    # ✅ NEW ACTION — product report
    @action(detail=True, methods=["get"], url_path="report")
    def report(self, request, pk=None):
        """
        Product-level daily sales summary (like the client's 'per-product report').
        Example: GET /api/products/23/report/?from=2025-11-01&to=2025-11-11
        """
        from_date = request.query_params.get("from")
        to_date = request.query_params.get("to")

        qs = OrderItem.objects.filter(product_id=pk, order__status="paid")

        if from_date:
            qs = qs.filter(order__created_at__date__gte=from_date)
        if to_date:
            qs = qs.filter(order__created_at__date__lte=to_date)

        daily = (
            qs.annotate(date=TruncDate("order__created_at"))
            .values("date")
            .annotate(total_qty=Sum("qty"), total_sales=Sum("line_total"))
            .order_by("-date")
        )

        totals = qs.aggregate(total_qty=Sum("qty"), total_sales=Sum("line_total"))

        return Response({
            "product_id": pk,
            "totals": totals,
            "daily": list(daily),
        })


    @action(detail=True, methods=["patch"], url_path=r"modifier-group/(?P<link_id>\d+)/toggle-title")
    def toggle_modifier_group_title(self, request, pk=None, link_id=None):
        """
        Toggle show_title for a ProductModifierGroup.
        """
        product = self.get_object()
        try:
            link = product.modifier_links.get(pk=link_id)
        except:
            return Response({"detail": "Modifier group not found"}, status=404)

        link.show_title = not link.show_title
        link.save(update_fields=["show_title"])

        return Response({
            "id": link.id,
            "group_name": link.group.name,
            "show_title": link.show_title,
        })
