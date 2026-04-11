from django.db import models
from django.db.models import Q  # 👈 add


class Category(models.Model):
    name = models.CharField(max_length=100)
    sort = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    class Meta: ordering = ["sort","name"]
    def __str__(self): return self.name 
    # thanks to me 

class Product(models.Model):
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="products")
    name = models.CharField(max_length=150)
    base_price = models.DecimalField(max_digits=10, decimal_places=2)
    kitchen_station = models.CharField(max_length=50, blank=True)
    image = models.ImageField(upload_to="products/", null=True, blank=True)
    is_active = models.BooleanField(default=True)

    sop_text = models.TextField(blank=True, default="")      # cooking steps / ratios
    sop_audio_url = models.URLField(blank=True, default="")  # optional voice SOP URL

    class Meta: indexes=[models.Index(fields=["category","is_active"])]
    def __str__(self): return self.name
    

class ProductVariant(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="variants")
    name = models.CharField(max_length=50)          # e.g., Large
    price_delta = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)   # 👈 archive instead of delete

    class Meta:
        constraints = [
            # Optional: avoid duplicate ACTIVE names per product
            models.UniqueConstraint(
                fields=["product","name"],
                condition=Q(is_active=True),
                name="uq_active_variant_name_per_product",
            )
        ]

    @property
    def effective_price(self):
        # Base + delta to use when creating order items
        return (self.product.base_price or 0) + (self.price_delta or 0)

class ModifierGroup(models.Model):
    SINGLE="single"; MULTI="multi"
    name = models.CharField(max_length=120)
    selection_type = models.CharField(max_length=6, choices=[(SINGLE,"single"),(MULTI,"multi")], default=MULTI)
    min_required = models.PositiveIntegerField(default=0)
    max_allowed = models.PositiveIntegerField(default=99)

class ModifierOption(models.Model):
    group = models.ForeignKey(ModifierGroup, on_delete=models.CASCADE, related_name="options")
    name = models.CharField(max_length=120)
    sort = models.PositiveIntegerField(default=0)
    price_delta = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    is_default = models.BooleanField(default=False)
    is_removable = models.BooleanField(default=True)
    multi_click = models.BooleanField(default=False)  # 👈 NEW: allow incrementing quantity

    class Meta:
        ordering = ["sort", "id"]

# class ProductModifierGroup(models.Model):
#     product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="modifier_links")
#     group = models.ForeignKey(ModifierGroup, on_delete=models.CASCADE, related_name="product_links")
#     show_title = models.BooleanField(default=True)  # 👈 NEW FIELD
#     show_group = models.BooleanField(default=True)

#     class Meta:
#         unique_together = ("product", "group")


class ProductModifierGroup(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="modifier_links")
    group = models.ForeignKey(ModifierGroup, on_delete=models.CASCADE, related_name="product_links")

    # UI behavior
    show_title = models.BooleanField(default=True)
    show_group = models.BooleanField(default=True)

    # ⭐ NEW (THIS IS THE KEY)
    required_variant = models.ForeignKey(
        "ProductVariant",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text="Only show this modifier group if this variant is selected"
    )
    
    required_options = models.ManyToManyField(
        "ModifierOption",
        blank=True,
        help_text="Only show this modifier group if ANY of these modifier options are selected (sub-modifier)"
    )

    class Meta:
        unique_together = ("product", "group")
