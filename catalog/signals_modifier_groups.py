# catalog/signals_modifier_groups.py
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Product, ModifierGroup, ProductModifierGroup

DEFAULT_GROUPS = [
    "Add",
    "Not Add",
    "Reduce",
]

@receiver(post_save, sender=Product)
def create_default_modifier_groups(sender, instance, created, **kwargs):
    # if not created:
    #     return

    # for name in DEFAULT_GROUPS:
    #     # 1) create ModifierGroup
    #     group = ModifierGroup.objects.create(
    #         name=name,
    #         selection_type="multi",
    #         min_required=0,
    #         max_allowed=99,
    #     )

    #     # 2) attach to Product
    #     ProductModifierGroup.objects.create(
    #         product=instance,
    #         group=group,
    #         show_title=True,  # default ON, boss can toggle later
    #     )
    return 
