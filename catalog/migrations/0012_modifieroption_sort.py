from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0011_remove_productmodifiergroup_required_option_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="modifieroption",
            name="sort",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AlterModelOptions(
            name="modifieroption",
            options={"ordering": ["sort", "id"]},
        ),
    ]
