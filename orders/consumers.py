from channels.generic.websocket import AsyncJsonWebsocketConsumer
import re

def _clean_station(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]", "_", (name or "MAIN").upper())[:80]

class KitchenConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        raw = self.scope["url_route"]["kwargs"]["station"]
        self.station = _clean_station(raw)
        self.group = f"kitchen.{self.station}"

        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        await self.channel_layer.group_discard(self.group, self.channel_name)

    async def kitchen_ticket(self, event):
        await self.send_json({"type": "ticket", "data": event["data"]})

    async def kitchen_update(self, event):
        await self.send_json({"type": "update", "data": event["data"]})

    async def kitchen_cancel(self, event):
        await self.send_json({"type": "cancel", "data": event["data"]})
