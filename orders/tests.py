from decimal import Decimal
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status
from django.contrib.auth.models import User
from catalog.models import Category, Product, ModifierGroup, ModifierOption, ProductModifierGroup
from orders.models import Order, OrderItem, OrderItemModifier, Table

class OrderAPITestCase(APITestCase):
    def setUp(self):
        # 1. Create a user and authenticate
        self.user = User.objects.create_user(username="testuser", password="password123")
        self.client.force_authenticate(user=self.user)

        # 2. Setup minimal catalog
        self.cat = Category.objects.create(name="Food")
        self.prod = Product.objects.create(
            category=self.cat, name="Test Burger", base_price=Decimal("10.00")
        )
        self.group = ModifierGroup.objects.create(name="Extra Info", selection_type="multi")
        # Link group to product
        ProductModifierGroup.objects.create(product=self.prod, group=self.group)
        
        self.opt_multi = ModifierOption.objects.create(
            group=self.group, name="Bacon", price_delta=Decimal("2.00"), multi_click=True
        )

    def test_create_order_with_multi_click_modifier(self):
        """Verify that creating an order with qty > 1 on a modifier calculates correctly."""
        url = reverse("order-list")
        data = {
            "payment_method": "cash",
            "tax_rate": "0.00",
            "items": [
                {
                    "product_id": self.prod.id,
                    "qty": 1,
                    "modifiers": [
                        {"option_id": self.opt_multi.id, "include": True, "qty": 3}
                    ]
                }
            ]
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        
        # Verify Price: 10.00 (Base) + (2.00 * 3) = 16.00
        order = Order.objects.get(pk=response.data['id'])
        item = order.items.first()
        self.assertEqual(item.unit_price, Decimal("16.00"))
        self.assertEqual(item.line_total, Decimal("16.00"))
        
        # Verify Modifier Qty in DB
        mod = item.modifiers.first()
        self.assertEqual(mod.qty, 3)

    def test_add_items_with_multi_click_modifier(self):
        """Verify that add_items endpoint correctly handles modifier quantities."""
        # Create an initial empty order
        order = Order.objects.create(payment_method="cash")
        # The endpoint is action 'add_items' on detail
        url = reverse("order-add-items", kwargs={"pk": order.id})
        
        data = {
            "tax_rate": "0.00",
            "items": [
                {
                    "product_id": self.prod.id,
                    "qty": 2,
                    "modifiers": [
                        {"option_id": self.opt_multi.id, "include": True, "qty": 2}
                    ]
                }
            ]
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Verify Price: 10.00 (Base) + (2.00 * 2) = 14.00 unit price
        # Line Total: 14.00 * 2 (item qty) = 28.00
        order.refresh_from_db()
        item = order.items.first()
        self.assertEqual(item.unit_price, Decimal("14.00"))
        self.assertEqual(item.line_total, Decimal("28.00"))
        self.assertEqual(order.total, Decimal("28.00"))

    def test_merge_items_with_same_modifiers(self):
        """Verify that items with the same multi-click modifier quantities are merged."""
        order = Order.objects.create(payment_method="cash")
        url = reverse("order-add-items", kwargs={"pk": order.id})
        
        item_data = {
            "product_id": self.prod.id,
            "qty": 1,
            "modifiers": [{"option_id": self.opt_multi.id, "include": True, "qty": 2}]
        }
        
        # Add first time
        self.client.post(url, {"tax_rate": "0.00", "items": [item_data]}, format='json')
        # Add second time (identical)
        self.client.post(url, {"tax_rate": "0.00", "items": [item_data]}, format='json')
        
        order.refresh_from_db()
        self.assertEqual(order.items.count(), 1)
        self.assertEqual(order.items.first().qty, 2)

    def test_attach_table_moves_order_and_updates_table_states(self):
        source = Table.objects.create(name="T1", status="occupied")
        dest = Table.objects.create(name="T2", status="free")
        order = Order.objects.create(table=source, table_name_snapshot=source.name, status="open")

        url = reverse("order-attach-table", kwargs={"pk": order.id})
        response = self.client.patch(url, {"table_id": dest.id}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        order.refresh_from_db()
        source.refresh_from_db()
        dest.refresh_from_db()

        self.assertEqual(order.table_id, dest.id)
        self.assertEqual(order.table_name_snapshot, dest.name)
        self.assertEqual(source.status, "free")
        self.assertEqual(dest.status, "occupied")
        self.assertEqual(response.data["table"]["id"], dest.id)
        self.assertEqual(response.data["table_display"], dest.name)

    def test_attach_table_rejects_destination_with_active_order(self):
        source = Table.objects.create(name="T3", status="occupied")
        dest = Table.objects.create(name="T4", status="occupied")
        order = Order.objects.create(table=source, table_name_snapshot=source.name, status="open")
        Order.objects.create(table=dest, table_name_snapshot=dest.name, status="open")

        url = reverse("order-attach-table", kwargs={"pk": order.id})
        response = self.client.patch(url, {"table_id": dest.id}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["detail"], "Destination table already has an active order.")

        order.refresh_from_db()
        source.refresh_from_db()
        dest.refresh_from_db()

        self.assertEqual(order.table_id, source.id)
        self.assertEqual(source.status, "occupied")
        self.assertEqual(dest.status, "occupied")
