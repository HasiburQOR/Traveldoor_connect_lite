from django.db import migrations

from django.db.models import Q


def rename_brand(apps, schema_editor):
    """Replace a saved "Traveldoor" company name with the Bin Omar Group brand."""
    SiteSettings = apps.get_model("core", "SiteSettings")
    SiteSettings.objects.filter(
        Q(company_name__icontains="traveldoor") | Q(company_name__iexact="Binomar Group")
    ).update(company_name="Bin Omar Group")


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(rename_brand, migrations.RunPython.noop),
    ]
