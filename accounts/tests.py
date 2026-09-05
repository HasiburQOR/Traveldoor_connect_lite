"""
Accounts tests — the branded login page, the "no Django admin" rule and the
self-service password-reset flow (request link by email, set new password).
"""
import re

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse

from notifications.models import NotificationLog

User = get_user_model()


class LoginTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="login1", password="pw-for-tests-123", email="login1@example.com",
            is_staff=True,
        )

    def test_login_page_renders_with_forgot_link(self):
        response = self.client.get(reverse("accounts:login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Welcome back")
        self.assertContains(response, reverse("accounts:password_reset"))

    def test_login_redirects_to_dashboard(self):
        response = self.client.post(reverse("accounts:login"), {
            "username": "login1", "password": "pw-for-tests-123",
        })
        self.assertRedirects(response, reverse("core:dashboard"))

    def test_login_accepts_the_email_address_too(self):
        response = self.client.post(reverse("accounts:login"), {
            "username": "LOGIN1@EXAMPLE.COM", "password": "pw-for-tests-123",
        })
        self.assertRedirects(response, reverse("core:dashboard"))

    def test_login_label_mentions_email(self):
        html = self.client.get(reverse("accounts:login")).content.decode()
        self.assertIn("Username or email", html)

    def test_wrong_credentials_show_error(self):
        response = self.client.post(reverse("accounts:login"), {
            "username": "login1", "password": "definitely-wrong",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "correct username and password")

    def test_django_admin_is_not_mounted(self):
        self.assertEqual(self.client.get("/admin/login/").status_code, 404)

    def test_protected_pages_redirect_to_custom_login_not_admin(self):
        for url in (reverse("core:dashboard"), reverse("accounts:user_list"),
                    reverse("events:list"), reverse("audit:list")):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302, f"{url} is not protected")
                self.assertTrue(response.url.startswith(reverse("accounts:login")),
                                f"{url} redirects to {response.url}, not the app login")
                self.assertNotIn("/admin", response.url)


class PasswordResetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="reset1", password="pw-for-tests-123", email="reset1@example.com",
            is_staff=True, first_name="Reset",
        )

    def _reset_link_from_outbox(self):
        """Pull the /accounts/password-reset/<uid>/<token>/ link out of the mail."""
        message = mail.outbox[0]
        html = message.alternatives[0][0]
        match = re.search(r"/accounts/password-reset/(?P<uid>[\w-]+)/(?P<token>[\w-]+)/", html)
        self.assertIsNotNone(match, "reset link missing from the email body")
        return match.group(0)

    def test_full_reset_flow(self):
        # 1. Request a link for a known email.
        response = self.client.post(reverse("accounts:password_reset"),
                                    {"email": "reset1@example.com"})
        self.assertRedirects(response, reverse("accounts:password_reset_done"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("reset", mail.outbox[0].subject.lower())

        # 2. The send is logged like every other outbound email.
        self.assertTrue(NotificationLog.objects.filter(
            type=NotificationLog.TYPE_PASSWORD_RESET,
            recipient_email="reset1@example.com",
            status=NotificationLog.STATUS_SENT,
        ).exists())

        # 3. Open the emailed link. Django first masks the one-time token by
        #    redirecting to a token-less "set-password" URL (token kept in the
        #    session), which is where the form lives.
        link = self._reset_link_from_outbox()
        response = self.client.get(link)
        self.assertEqual(response.status_code, 302)
        form_url = response.url
        response = self.client.get(form_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Choose a new password")

        response = self.client.post(form_url, {
            "new_password1": "AWholeNewPass!9", "new_password2": "AWholeNewPass!9",
        })
        self.assertRedirects(response, reverse("accounts:password_reset_complete"))

        # 4. Old password is dead, new one signs in.
        self.assertFalse(self.client.login(username="reset1", password="pw-for-tests-123"))
        self.assertTrue(self.client.login(username="reset1", password="AWholeNewPass!9"))

    def test_link_only_works_once(self):
        self.client.post(reverse("accounts:password_reset"), {"email": "reset1@example.com"})
        link = self._reset_link_from_outbox()
        form_url = self.client.get(link).url  # token-less form URL
        self.client.post(form_url, {"new_password1": "AWholeNewPass!9",
                                    "new_password2": "AWholeNewPass!9"})
        response = self.client.get(link)  # the original token link is dead now
        self.assertContains(response, "expired")

    def test_unknown_email_sends_nothing_but_still_says_check_inbox(self):
        response = self.client.post(reverse("accounts:password_reset"),
                                    {"email": "nobody@example.com"})
        self.assertRedirects(response, reverse("accounts:password_reset_done"))
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(
            NotificationLog.objects.filter(type=NotificationLog.TYPE_PASSWORD_RESET).exists()
        )

    def test_reset_pages_render_signed_out(self):
        for name in ("password_reset", "password_reset_done", "password_reset_complete"):
            with self.subTest(name=name):
                response = self.client.get(reverse(f"accounts:{name}"))
                self.assertEqual(response.status_code, 200)
