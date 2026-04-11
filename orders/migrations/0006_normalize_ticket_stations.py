from django.db import migrations
import re

_ST_RE = re.compile(r"\s+")

def _clean(name: str) -> str:
    s = _ST_RE.sub(" ", (name or "MAIN").strip()).upper()
    return re.sub(r"[^0-9A-Z._-]", "_", s)[:80]

def forwards(apps, schema_editor):
    KitchenTicket = apps.get_model("orders", "KitchenTicket")
    for t in KitchenTicket.objects.iterator():
        cs = _clean(t.station)
        if t.station != cs:
            KitchenTicket.objects.filter(pk=t.pk).update(station=cs)

class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0005_order_void_reason_order_voided_at_order_voided_by"),  # ⚠️ replace with your latest migration
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
