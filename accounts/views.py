"""
Account views — admin login handled by Django auth views; user management
(create/edit admins, welcome email with credentials) per FR-8.2 and SRS 1.2.
"""
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from audit.models import AuditLogEntry, log_action
from notifications.emails import send_admin_account_email

from .forms import AdminUserCreationForm, AdminUserEditForm

User = get_user_model()


@staff_member_required
def user_list(request):
    users = User.objects.order_by("username")
    return render(request, "accounts/user_list.html", {"users": users})


@staff_member_required
def user_create(request):
    """FR-8.2 / SRS 1.2 — create an admin account and email credentials."""
    form = AdminUserCreationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        raw_password = form.cleaned_data["password1"]
        user = form.save()
        log_action(request.user, AuditLogEntry.ACTION_CREATE, user,
                   details=f"Admin account created for {user.email}.",
                   entity_type=AuditLogEntry.ENTITY_ADMIN_ACCOUNT)
        send_admin_account_email(user, raw_password)
        messages.success(request, f"Account created — credentials emailed to {user.email}.")
        return redirect("accounts:user_list")
    return render(request, "accounts/user_form.html", {"form": form, "title": "New admin user"})


@staff_member_required
def user_edit(request, pk):
    user = get_object_or_404(User, pk=pk)
    form = AdminUserEditForm(request.POST or None, instance=user)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        log_action(request.user, AuditLogEntry.ACTION_UPDATE, user,
                   details="Admin account updated.", entity_type=AuditLogEntry.ENTITY_ADMIN_ACCOUNT)
        messages.success(request, "Account updated.")
        return redirect("accounts:user_list")
    return render(request, "accounts/user_form.html", {"form": form, "title": f"Edit — {user.username}"})
