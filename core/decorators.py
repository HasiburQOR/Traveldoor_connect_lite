"""
Shared view decorators.

Django's own ``staff_member_required`` (django.contrib.admin) redirects
unauthenticated users to the Django admin login screen — and this back office
deliberately has exactly one front door: the branded ``/accounts/login/``
page. The decorator below keeps the identical staff check but sends people
there instead (and to its password-reset flow when they forget credentials).
"""
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.decorators import user_passes_test


def staff_member_required(view_func=None,
                          redirect_field_name=REDIRECT_FIELD_NAME,
                          login_url="accounts:login"):
    """Staff-only view that redirects signed-out users to the app login page."""
    actual_decorator = user_passes_test(
        lambda u: u.is_active and u.is_staff,
        login_url=login_url,
        redirect_field_name=redirect_field_name,
    )
    if view_func:
        return actual_decorator(view_func)
    return actual_decorator
