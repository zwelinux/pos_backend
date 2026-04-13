from django.db import migrations


def normalize_tab_payment_method(apps, schema_editor):
    Order = apps.get_model("orders", "Order")
    Order.objects.filter(status="tab", payment_method="tab").update(payment_method="pending")


def restore_tab_payment_method(apps, schema_editor):
    Order = apps.get_model("orders", "Order")
    Order.objects.filter(status="tab", payment_method="pending").update(payment_method="tab")


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0022_alter_kitchenticket_status"),
    ]

    operations = [
        migrations.RunPython(normalize_tab_payment_method, restore_tab_payment_method),
    ]
