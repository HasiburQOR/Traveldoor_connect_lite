from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0003_alter_slot_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="active_weekdays",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Comma-separated weekdays the event runs on, 0=Mon … 6=Sun. "
                    "Blank = every day."
                ),
                max_length=20,
            ),
        ),
    ]
