from django.db import migrations
import re

_ST_RE = re.compile(r"\s+")

def _clean(name: str) -> str:
    s = _ST_RE.sub(" ", (name or "MAIN").strip()).upper()
    return re.sub(r"[^0-9A-Z._-]", "_", s)[:80]

def forwards(apps, schema_editor):
    Product = apps.get_model("catalog", "Product")
    for p in Product.objects.iterator():
        cs = _clean(p.kitchen_station)
        if p.kitchen_station != cs:
            Product.objects.filter(pk=p.pk).update(kitchen_station=cs)

class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0003_normalize_product_stations"),  # ⚠️ replace with your latest migration name
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
