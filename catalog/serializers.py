from rest_framework import serializers
from .models import Category, Product, ProductVariant, ModifierGroup, ModifierOption, ProductModifierGroup

class CategorySer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ("id", "name", "sort")

class ProductVariantSer(serializers.ModelSerializer):
    class Meta:
        model = ProductVariant
        fields = ("id", "name", "price_delta", "is_active")

class ModifierOptionSer(serializers.ModelSerializer):
    class Meta:
        model = ModifierOption
        fields = ("id", "name", "sort", "price_delta", "is_default", "is_removable", "multi_click")

class ModifierGroupSer(serializers.ModelSerializer):
    options = ModifierOptionSer(many=True, read_only=True)
    class Meta:
        model = ModifierGroup
        fields = ("id", "name", "selection_type", "min_required", "max_allowed", "options")

class ModifierGroupSimpleSer(serializers.ModelSerializer):
    show_title = serializers.BooleanField()
    show_group = serializers.BooleanField()

    class Meta:
        model = ProductModifierGroup
        fields = ("id", "name", "selection_type", "min_required", "max_allowed", "show_title", "show_group")


class ProductListSer(serializers.ModelSerializer):
    category = CategorySer(read_only=True)
    class Meta:
        model = Product
        fields = ("id", "name", "base_price", "image", "category")


class ProductModifierGroupSer(serializers.ModelSerializer):
    name = serializers.CharField(source="group.name", read_only=True)
    selection_type = serializers.CharField(source="group.selection_type", read_only=True)
    min_required = serializers.IntegerField(source="group.min_required", read_only=True)
    max_allowed = serializers.IntegerField(source="group.max_allowed", read_only=True)

    required_variant_id = serializers.IntegerField(read_only=True)
    required_option_ids = serializers.SerializerMethodField()

    options = serializers.SerializerMethodField()

    class Meta:
        model = ProductModifierGroup
        fields = (
            "id",
            "name",
            "selection_type",
            "min_required",
            "max_allowed",
            "show_title",
            "show_group",
            "required_variant_id",
            "required_option_ids",
            "options",
        )

    def get_options(self, obj):
        return ModifierOptionSer(
            obj.group.options.all(), many=True
        ).data

    def get_required_option_ids(self, obj):
        return list(obj.required_options.values_list('id', flat=True))


class ProductDetailSer(serializers.ModelSerializer):
    category = CategorySer(read_only=True)  # optional to show the category on detail
    variants = ProductVariantSer(many=True, read_only=True)
    modifier_groups = ProductModifierGroupSer(
        source="modifier_links", many=True, read_only=True
    )
    class Meta:
        model = Product
        fields = ("id", "name", "base_price", "image", "category", "variants", "modifier_groups")

    def get_modifier_groups(self, obj):
        """
        Use the prefetched relation (modifier_links__group__options) so there are no extra queries.
        """
        groups = []
        seen = set()
        # obj.modifier_links is prefetched by the view; each link has .group with .options prefetched
        for link in getattr(obj, "modifier_links").all():
            g = link.group
            if g and g.id not in seen:
                seen.add(g.id)
                groups.append(g)
        return ModifierGroupSer(groups, many=True).data
