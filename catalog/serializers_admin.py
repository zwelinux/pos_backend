# catalog/serializers_admin.py
from rest_framework import serializers
from django.db import transaction
from django.db.models import Q
from .models import (
    Product, ProductVariant, Category,
    ModifierGroup, ModifierOption, ProductModifierGroup
)
from orders.models import OrderItemModifier
import re
from decimal import Decimal
from .image_utils import process_product_image
import json


def _clean_station(name: str) -> str:
    s = re.sub(r"\s+", " ", (name or "MAIN").strip()).upper()
    return re.sub(r"[^0-9A-Z._-]", "_", s)[:80]


# =========================
# Categories
# =========================
class CategorySimpleSer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ("id", "name")


class CategoryAdminSer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ("id", "name", "sort", "is_active")

    def validate_name(self, v):
        v = re.sub(r"\s+", " ", (v or "").strip())
        if not v:
            raise serializers.ValidationError("Name is required.")
        return v

    def create(self, validated):
        if not validated.get("sort"):
            last = Category.objects.order_by("-sort").values_list("sort", flat=True).first() or 0
            validated["sort"] = last + 1
        return super().create(validated)


# =========================
# Variants
# =========================
class ProductVariantInSer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False, allow_null=True)
    is_active = serializers.BooleanField(required=False)

    class Meta:
        model = ProductVariant
        fields = ("id", "name", "price_delta", "is_active")

class ModifierGroupSimpleSer(serializers.ModelSerializer):
    class Meta:
        model = ModifierGroup
        fields = ("id", "name", "selection_type", "min_required", "max_allowed")

# =========================
# Product Admin
# =========================
class ProductSimpleSer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = ("id", "name")


class ProductAdminSer(serializers.ModelSerializer):
    category = serializers.PrimaryKeyRelatedField(
        queryset=Category.objects.all(), write_only=True
    )
    category_obj = CategorySimpleSer(source="category", read_only=True)

    variants = serializers.SerializerMethodField(read_only=True)

    modifier_group_ids = serializers.JSONField(required=False, write_only=True)
    modifier_groups = serializers.SerializerMethodField(read_only=True)
    modifier_links = serializers.JSONField(required=False, write_only=True)

    class Meta:
        model = Product
        fields = (
            "id", "category", "category_obj", "name", "base_price",
            "kitchen_station", "image", "is_active", "sop_text", "sop_audio_url",
            "variants", "modifier_group_ids", "modifier_groups", "modifier_links",
        )


    def to_internal_value(self, data):
        # MultiPartParser handles these fields as strings if sent via FormData.
        # We'll allow them to be passed as JSON strings and parse them here.
        if hasattr(data, 'dict'):
            data = data.dict()
        else:
            data = dict(data)

        for field in ['variants', 'modifier_group_ids', 'modifier_links']:
            val = data.get(field)
            if isinstance(val, str) and val.strip():
                try:
                    data[field] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    # We'll let the field-level validation handle it if it's still a string
                    pass

        validated = super().to_internal_value(data)

        # `variants` is exposed read-only in responses so edit forms can preload
        # existing variant rows, but we still accept incoming variant payloads here.
        if "variants" in data:
          validated["variants"] = data.get("variants") or []

        return validated

    def get_variants(self, obj):
        return [
            {
                "id": v.id,
                "name": v.name,
                "price_delta": v.price_delta,
                "is_active": v.is_active,
            }
            for v in obj.variants.all().order_by("id")
        ]

    # ⭐⭐⭐ READ (POS + Admin Edit)
    def get_modifier_groups(self, obj):
        links = ProductModifierGroup.objects.filter(product=obj).select_related("group")
        return [
            {
                "id": link.group.id,
                "name": link.group.name,
                "selection_type": link.group.selection_type,
                "min_required": link.group.min_required,
                "max_allowed": link.group.max_allowed,
                "show_title": link.show_title,
                "show_group": link.show_group,

                "required_variant": link.required_variant_id,
                "required_options": list(link.required_options.values_list('id', flat=True)),

                "ui": {
                    "show_title": link.show_title,
                    "show_group": link.show_group,
                    "required_variant": link.required_variant_id,
                    "required_options": list(link.required_options.values_list('id', flat=True)),
                }
            }
            for link in links
        ]

    # ⭐⭐⭐ WRITE (THIS WAS MISSING)
    def _sync_modifier_links(self, product, ids, ui_settings=None):
        ui_settings = ui_settings or {}

        wanted = set(ids or [])
        current_links = {pl.group_id: pl for pl in product.modifier_links.all()}

        # CREATE
        for gid in wanted - set(current_links.keys()):
            ui = ui_settings.get(str(gid), {})
            link = ProductModifierGroup.objects.create(
                product=product,
                group_id=gid,
                show_title=ui.get("show_title", True),
                show_group=ui.get("show_group", True),
                required_variant_id=ui.get("required_variant"),
            )
            ro = ui.get("required_options")
            if ro:
                link.required_options.set(ro)

        # UPDATE
        for gid in wanted & set(current_links.keys()):
            link = current_links[gid]
            s = ui_settings.get(str(gid), {})

            if not s:
                continue

            link.show_title = s.get("show_title", link.show_title)
            link.show_group = s.get("show_group", link.show_group)

            if "required_variant" in s:
                rv = s.get("required_variant")
                if rv:
                    if product.variants.filter(id=rv).exists():
                        link.required_variant_id = rv
                    else:
                        link.required_variant_id = None
                else:
                    link.required_variant_id = None

            link.save(update_fields=[
                "show_title",
                "show_group",
                "required_variant_id",
            ])

            if "required_options" in s:
                link.required_options.set(s.get("required_options") or [])



        # DELETE UNUSED
        ProductModifierGroup.objects.filter(
            product=product,
            group_id__in=set(current_links.keys()) - wanted
        ).delete()

    # =========================
    # Create / Update
    # =========================
    @transaction.atomic
    def create(self, validated):
        vs = validated.pop("variants", [])
        mg_ids = validated.pop("modifier_group_ids", [])
        ui_settings = validated.pop("modifier_links", {})

        if "kitchen_station" in validated:
            validated["kitchen_station"] = _clean_station(validated["kitchen_station"])

        if "image" in validated and validated["image"]:
            validated["image"] = process_product_image(validated["image"])

        product = Product.objects.create(**validated)
        self._replace_variants(product, vs)
        self._sync_modifier_links(product, mg_ids, ui_settings)
        return product

    @transaction.atomic
    def update(self, instance, validated):
        vs = validated.pop("variants", None)
        mg_ids = validated.pop("modifier_group_ids", None)
        ui_settings = validated.pop("modifier_links", {})

        if "kitchen_station" in validated:
            validated["kitchen_station"] = _clean_station(validated["kitchen_station"])

        if "image" in validated and validated["image"]:
            validated["image"] = process_product_image(validated["image"])

        for k, v in validated.items():
            setattr(instance, k, v)
        instance.save()

        if vs is not None:
            self._replace_variants(instance, vs)
        if mg_ids is not None:
            self._sync_modifier_links(instance, mg_ids, ui_settings)
        return instance

    def validate_kitchen_station(self, v):
        return _clean_station(v)

    # =========================
    # Variant Sync (unchanged)
    # =========================
    def _replace_variants(self, product, vs):
        """
        HARD sync variants:
        - Removed variants are DELETED
        - ModifierGroup.required_variant is auto-cleared if orphaned
        """

        existing = {pv.id: pv for pv in product.variants.all()}
        incoming_ids = {v.get("id") for v in vs if v.get("id")}

        # 1️⃣ DELETE removed variants (REAL removal)
        removed_ids = set(existing.keys()) - incoming_ids
        if removed_ids:
            # clear modifier-group dependencies
            ProductModifierGroup.objects.filter(
                product=product,
                required_variant_id__in=removed_ids
            ).update(required_variant=None)

            ProductVariant.objects.filter(id__in=removed_ids).delete()

        # 2️⃣ UPSERT remaining variants
        for v in vs:
            vid = v.get("id")
            name = (v.get("name") or "").strip()
            price_delta = Decimal(str(v.get("price_delta") or 0))

            if not name:
                continue

            if vid and vid in existing:
                pv = existing[vid]
                pv.name = name
                pv.price_delta = price_delta
                pv.is_active = True
                pv.save(update_fields=["name", "price_delta", "is_active"])
            else:
                ProductVariant.objects.create(
                    product=product,
                    name=name,
                    price_delta=price_delta,
                    is_active=True,
                )


# =========================
# Modifier Groups & Options
# =========================
class ModifierOptionSer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False, allow_null=True)

    class Meta:
        model = ModifierOption
        fields = ("id", "name", "sort", "price_delta", "is_default", "is_removable", "multi_click")


class ModifierGroupAdminSer(serializers.ModelSerializer):
    options = ModifierOptionSer(many=True, required=False)
    product_ids = serializers.ListField(
        child=serializers.IntegerField(), write_only=True, required=False
    )
    active_products = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = ModifierGroup
        fields = (
            "id", "name", "selection_type", "min_required", "max_allowed", 
            "options", "product_ids", "active_products"
        )

    def get_active_products(self, obj):
        # returns simple list of products this group is currently attached to
        links = obj.product_links.select_related("product")
        return [
            {"id": link.product.id, "name": link.product.name}
            for link in links
        ]

    def validate(self, attrs):
        sel = attrs.get("selection_type", getattr(self.instance, "selection_type", ModifierGroup.MULTI))
        min_r = attrs.get("min_required", getattr(self.instance, "min_required", 0))
        max_a = attrs.get("max_allowed", getattr(self.instance, "max_allowed", 99))
        if min_r > max_a:
            raise serializers.ValidationError("min_required cannot be greater than max_allowed.")
        if sel == ModifierGroup.SINGLE and max_a < 1:
            raise serializers.ValidationError("For 'single', max_allowed must be at least 1.")
        return attrs

    def _upsert_options_and_delete_truly_unused(self, group, incoming_opts):
        incoming = incoming_opts or []
        existing = {o.id: o for o in group.options.all()}
        seen_ids = set()

        for sort, row in enumerate(incoming):
            oid = row.get("id")
            name = (row.get("name") or "").strip() or "Option"
            price_delta = row.get("price_delta") or 0
            is_default = bool(row.get("is_default", False))
            is_removable = bool(row.get("is_removable", True))
            multi_click = bool(row.get("multi_click", False))
            if oid and oid in existing:
                o = existing[oid]
                o.name = name
                o.sort = sort
                o.price_delta = price_delta
                o.is_default = is_default
                o.is_removable = is_removable
                o.multi_click = multi_click
                o.save(update_fields=[
                    "name",
                    "sort",
                    "price_delta",
                    "is_default",
                    "is_removable",
                    "multi_click",
                ])
                seen_ids.add(o.id)
            else:
                ModifierOption.objects.create(
                    group=group,
                    name=name,
                    sort=sort,
                    price_delta=price_delta,
                    is_default=is_default,
                    is_removable=is_removable,
                    multi_click=multi_click,
                )

        to_consider_delete = [oid for oid in existing.keys() if oid not in seen_ids]
        if to_consider_delete:
            used_ids = set(
                OrderItemModifier.objects
                .filter(option_id__in=to_consider_delete)
                .values_list("option_id", flat=True)
            )
            deletable_ids = set(to_consider_delete) - used_ids
            ModifierOption.objects.filter(id__in=deletable_ids).delete()


    def _sync_product_links(self, group, product_ids):
        if product_ids is None:
            return

        wanted = set(product_ids)
        current = set(group.product_links.values_list("product_id", flat=True))

        # CREATE / UPDATE
        for pid in wanted:
            ProductModifierGroup.objects.get_or_create(
                product_id=pid,
                group=group,
                defaults={
                    "show_title": True,
                    "show_group": True,
                }
            )

        # DELETE removed ones
        removed = current - wanted
        if removed:
            group.product_links.filter(product_id__in=removed).delete()

    @transaction.atomic
    def create(self, validated_data):
        options = validated_data.pop("options", [])
        product_ids = validated_data.pop("product_ids", None)
        
        group = ModifierGroup.objects.create(**validated_data)
        self._upsert_options_and_delete_truly_unused(group, options)
        self._sync_product_links(group, product_ids)
        return group

    @transaction.atomic
    def update(self, instance, validated_data):
        options = validated_data.pop("options", None)
        product_ids = validated_data.pop("product_ids", None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if options is not None:
            self._upsert_options_and_delete_truly_unused(instance, options)
        
        if product_ids is not None:
            self._sync_product_links(instance, product_ids)

        return instance
