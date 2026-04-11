import re
from django.db.models.signals import pre_save
from django.dispatch import receiver
from catalog.models import Product
from orders.models import KitchenTicket

_ST_RE = re.compile(r"\s+")

def _clean_station(name: str) -> str:
    s = _ST_RE.sub(" ", (name or "MAIN").strip()).upper()
    return re.sub(r"[^0-9A-Z._-]", "_", s)[:80]

@receiver(pre_save, sender=Product)
def product_station_normalizer(sender, instance: Product, **kwargs):
    instance.kitchen_station = _clean_station(getattr(instance, "kitchen_station", ""))

@receiver(pre_save, sender=KitchenTicket)
def ticket_station_normalizer(sender, instance: KitchenTicket, **kwargs):
    instance.station = _clean_station(getattr(instance, "station", ""))
