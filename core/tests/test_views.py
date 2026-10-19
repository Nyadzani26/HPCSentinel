"""Sign-in and session behaviour (FR-01, NFR-01, NFR-04)."""
from django.test import TestCase
from django.urls import reverse

from core.tests.helpers import make_user


class AccessTests(TestCase):
    def test_home_requires_sign_in(self):  # FR-01
        response = self.client.get(reverse("home"))
        self.assertRedirects(response, f"{reverse('login')}?next=/")

    def test_sign_in_with_valid_credentials(self):
        make_user("op")
        self.assertTrue(self.client.login(username="op", password="correct-horse-battery"))
        self.assertContains(self.client.get(reverse("home")), "Overview")

    def test_wrong_password_is_refused_without_naming_the_cause(self):
        make_user("op")
        response = self.client.post(reverse("login"), {"username": "op", "password": "wrong"})
        self.assertContains(response, "The username or password is not correct.")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_deactivated_user_cannot_sign_in(self):
        make_user("gone", active=False)
        self.assertFalse(self.client.login(username="gone", password="correct-horse-battery"))

    def test_session_cookie_is_not_readable_by_scripts(self):
        from django.conf import settings
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)
        self.assertEqual(settings.SESSION_COOKIE_AGE, settings.SESSION_IDLE_MINUTES * 60)

    def test_health_endpoint(self):
        self.assertEqual(self.client.get(reverse("health")).json(), {"status": "ok"})


class ProxySettingsTests(TestCase):
    def test_cookie_names_are_isolated(self):
        from django.conf import settings
        self.assertEqual(settings.SESSION_COOKIE_NAME, "hpcsentinel_sessionid")
        self.assertEqual(settings.CSRF_COOKIE_NAME, "hpcsentinel_csrftoken")

    def test_cookie_paths_end_with_slash(self):
        from django.conf import settings
        self.assertTrue(settings.SESSION_COOKIE_PATH.endswith("/"))
        self.assertTrue(settings.CSRF_COOKIE_PATH.endswith("/"))
