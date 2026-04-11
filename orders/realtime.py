# orders/realtime.py
import re
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

_ST_RE = re.compile(r"\s+")

def _clean_station(name: str) -> str:
    s = _ST_RE.sub(" ", (name or "MAIN").strip()).upper()
    return re.sub(r"[^0-9A-Z._-]", "_", s)[:80]

# orders/realtime.py
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from catalog.utils import clean_station


# orders/realtime.py

from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from .models import KitchenTicket

def notify_kds(ticket: KitchenTicket, event="ticket"):
    layer = get_channel_layer()
    station = ticket.station  # already normalized

    payload = {
        "type": event,
        "data": {
            "id": ticket.id,
            "order_id": ticket.order_id,
            "item_id": ticket.item_id,
            "station": ticket.station,
            "qty": ticket.qty,
            "status": ticket.status,
            "product_name": ticket.product_name,
            "variant_name": ticket.variant_name,
            "table_name": ticket.table_name,
            "modifiers": ticket.modifiers or [],
            "created_at": ticket.created_at.isoformat(),
            "started_at": ticket.started_at.isoformat() if ticket.started_at else None,
            "done_at": ticket.done_at.isoformat() if ticket.done_at else None,
        }
    }

    # Send to specific station
    async_to_sync(layer.group_send)(f"kitchen.{station}", payload)

    # Send to ALL-stations dashboard
    async_to_sync(layer.group_send)("kitchen.ALL", payload)
