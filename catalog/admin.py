# catalog/admin.py
from django.contrib import admin
from django.utils.html import format_html_join, format_html
from .models import (
    Category,
    Product,
    ProductVariant,
    ModifierGroup,
    ModifierOption,
    ProductModifierGroup,
)

# ---------- Inlines ----------

class ProductVariantInline(admin.TabularInline):
    model = ProductVariant
    fk_name = "product"              # ensure Django links to Product correctly
    extra = 1
    fields = ("name", "price_delta")


class ModifierOptionInline(admin.TabularInline):
    model = ModifierOption
    extra = 2
    fields = ("name", "price_delta")
    show_change_link = True


class ProductModifierGroupInline(admin.TabularInline):
    """
    Attach ModifierGroup(s) to a Product via the through model.
    Shows a small preview of options in that group so you don't need to click away.
    """
    model = ProductModifierGroup
    fk_name = "product"
    extra = 0
    autocomplete_fields = ("group", "required_variant", "required_options")
    readonly_fields = ("group_options_preview",)
    fields = ("group", "required_variant", "required_options", "show_group", "show_title", "group_options_preview")

    def group_options_preview(self, obj):
        if not obj or not obj.group_id:
            return "-"
        qs = ModifierOption.objects.filter(group=obj.group).only("name", "price_delta").order_by("id")
        if not qs.exists():
            return format_html('<span style="color:#888">No options</span>')
        return format_html_join(
            "", "<div>• {} ({:+g})</div>",
            ((o.name, o.price_delta or 0) for o in qs)
        )
    group_options_preview.short_description = "Options in group"


# ---------- ModelAdmins ----------

@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    search_fields = ("name",)


@admin.register(ModifierGroup)
class ModifierGroupAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "options_count")
    search_fields = ("name",)
    inlines = [ModifierOptionInline]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.prefetch_related("options")

    def options_count(self, obj):
        return obj.options.count()
    options_count.short_description = "Options"


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "base_price", "kitchen_station", "category")
    list_filter = ("category",)
    search_fields = ("name",)
    list_select_related = ("category",)
    save_on_top = True
    inlines = [
        ProductVariantInline,        # now variants will show correctly
        ProductModifierGroupInline,  # attach groups and preview their options
    ]


@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    list_display = ("id", "product", "name", "price_delta")
    search_fields = ("name", "product__name")
    autocomplete_fields = ("product",)
    list_select_related = ("product",)


@admin.register(ModifierOption)
class ModifierOptionAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "group", "price_delta")
    list_filter = ("group",)
    search_fields = ("name", "group__name")
    list_select_related = ("group",)


@admin.register(ProductModifierGroup)
class ProductModifierGroupAdmin(admin.ModelAdmin):
    list_display = ("id", "product", "group")
    list_filter = ("group", "product")
    search_fields = ("product__name", "group__name")
    autocomplete_fields = ("product", "group")
    list_select_related = ("product", "group")

admin.site.site_header = 'JUS POS'
admin.site.site_title = 'JUS POS'