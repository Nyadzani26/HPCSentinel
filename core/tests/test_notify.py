"""Email alerts (FR-31): what is sent, what is deliberately not, and that a mail problem never reaches detection."""
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from core.models import Alert, ClusterEvent, Node, Notification, PlatformSetting, User
from core.services import notify
from core.tests.helpers import make_alert, make_node, make_user

EXAMPLE = str(Path(__file__).resolve().parents[2] / "config" / "hpcsentinel.example.yml")
PASSWORD = "s3cret-app-password"
READY = dict(EMAIL_HOST="smtp.test", EMAIL_PORT=587, EMAIL_HOST_PASSWORD=PASSWORD, DEFAULT_FROM_EMAIL="sentinel@test",
             SITE_URL="https://sentinel.test")


def explanation(metric="nfs_request_rate", observed=300.0, expected=2.0):
    return {"summary": "x", "contributors": [{"metric": metric, "feature": f"{metric}__mean_5", "compared_with": "own baseline",
                                              "observed": observed, "expected": expected, "deviation_pct": 14900.0, "z": 10.0}]}


@override_settings(**READY)
class SweepTests(TestCase):
    def setUp(self):
        self.admin = make_user("boss", role=User.Role.ADMINISTRATOR)

    def turn_on(self, recipients="gift@spu.test", events=True, now=None):
        return notify.save_settings(self.admin, True, recipients, events, now=now or timezone.now())

    def alert(self, name="compute07", opened=None, **kw):
        a = make_alert(node=make_node(name), **kw)
        Alert.objects.filter(pk=a.pk).update(opened_at=opened or timezone.now(), explanation=explanation())
        a.refresh_from_db()
        return a

    def event(self, nodes=14, ended=False, **kw):
        now = timezone.now()
        return ClusterEvent.objects.create(started_at=now, last_seen_at=now, ended_at=now if ended else None, peak_nodes=nodes,
                                           peak_reliable=47, nodes=[f"compute{i:02d}" for i in range(1, nodes + 1)],
                                           causes={"nfs_request_rate": nodes - 2, "cpu_busy": 2}, **kw)

    # --- when nothing is sent -----------------------------------------------------------------------------------

    def test_nothing_is_sent_while_it_is_off(self):
        self.alert()
        self.assertIsNone(notify.sweep())
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(EMAIL_HOST="")
    def test_nothing_is_sent_and_nothing_breaks_when_the_server_is_not_set_up(self):
        self.turn_on()
        a = self.alert()
        self.assertIsNone(notify.sweep())
        self.assertEqual((len(mail.outbox), Notification.objects.count()), (0, 0))
        self.assertIsNone(Alert.objects.get(pk=a.pk).notified_at)

    def test_turning_it_on_never_sends_a_backlog(self):
        self.alert(opened=timezone.now() - timedelta(minutes=5))              # already open when it was switched on
        self.turn_on()
        self.assertIsNone(notify.sweep())
        self.assertEqual(len(mail.outbox), 0)
        self.alert("compute08")                                              # one that opens afterwards is sent
        notify.sweep()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("compute08", mail.outbox[0].subject)

    def test_only_a_new_active_live_alert_is_news(self):
        now = timezone.now()
        self.turn_on(now=now - timedelta(hours=3))
        self.alert("compute01", status=Alert.Status.ACKNOWLEDGED)               # someone is already looking
        self.alert("compute02", status=Alert.Status.RESOLVED)                   # it ended before anyone was told
        self.alert("compute03", mode=Alert.Mode.REPLAY)                         # a replay, not the cluster
        self.alert("compute04", opened=now - timedelta(hours=2))                # no longer new
        self.assertIsNone(notify.sweep(now))
        self.assertEqual(len(mail.outbox), 0)
        self.alert("compute05")
        notify.sweep(now)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("compute05", mail.outbox[0].subject)
        for other in ("compute01", "compute02", "compute03", "compute04"):
            self.assertNotIn(other, mail.outbox[0].body)

    # --- what is sent ---------------------------------------------------------------------------------------------

    def test_a_new_alert_is_emailed_once_with_a_link(self):
        self.turn_on()
        a = self.alert()
        record = notify.sweep()
        self.assertTrue(record.ok)
        self.assertEqual((record.alerts, record.events, record.kind), (1, 0, Notification.Kind.ALERTS))
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual((message.to, message.from_email), (["gift@spu.test"], "sentinel@test"))
        self.assertIn("compute07", message.subject)
        self.assertIn(f"https://sentinel.test/alerts/{a.pk}/", message.body)
        self.assertNotIn(PASSWORD, message.body + message.subject)
        self.assertIsNotNone(Alert.objects.get(pk=a.pk).notified_at)
        self.assertIsNone(notify.sweep())                                        # the same alert is never sent again
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(SITE_URL="")
    def test_without_a_site_address_the_email_has_no_link(self):
        self.turn_on()
        self.alert()
        notify.sweep()
        self.assertNotIn("http", mail.outbox[0].body)

    def test_alerts_that_open_together_share_one_email(self):
        self.turn_on()
        for name in ("compute07", "compute08", "compute09"):
            self.alert(name)
        record = notify.sweep()
        self.assertEqual(record.alerts, 3)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "HPCSentinel: 3 new alerts")
        for name in ("compute07", "compute08", "compute09"):
            self.assertIn(name, mail.outbox[0].body)

    def test_an_alert_that_ends_and_comes_back_is_not_sent_twice(self):
        self.turn_on()
        a = self.alert()
        notify.sweep()
        a.resolve(timezone.now(), "recovered")
        a.flare_up(timezone.now())
        self.assertIsNone(notify.sweep())
        self.assertEqual(len(mail.outbox), 1)

    def test_a_cluster_wide_event_is_one_email_not_one_per_node(self):
        self.turn_on()
        e = self.event()
        record = notify.sweep()
        self.assertEqual((record.alerts, record.events), (0, 1))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("14 nodes", mail.outbox[0].subject)
        self.assertIn("nfs requests", mail.outbox[0].body)
        self.assertIn(f"https://sentinel.test/events/{e.pk}/", mail.outbox[0].body)
        self.assertIsNone(notify.sweep())

    def test_events_can_be_left_out(self):
        self.turn_on(events=False)
        e = self.event()
        self.assertIsNone(notify.sweep())
        self.assertIsNone(ClusterEvent.objects.get(pk=e.pk).notified_at)
        self.alert()
        notify.sweep()
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("event", mail.outbox[0].subject)

    def test_an_event_that_has_already_ended_is_not_news(self):
        self.turn_on()
        self.event(ended=True)
        self.assertIsNone(notify.sweep())

    def test_an_alert_and_an_event_share_one_email(self):
        self.turn_on()
        self.alert()
        self.event()
        notify.sweep()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "HPCSentinel: 1 new alert and 1 cluster-wide event")

    def test_a_node_name_can_never_add_a_header(self):
        self.turn_on()
        node = Node.objects.create(name="compute07\r\nBcc: evil@x.test", source_label="x:9100")
        a = make_alert(node=node)
        Alert.objects.filter(pk=a.pk).update(opened_at=timezone.now(), explanation=explanation())
        notify.sweep()
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("\n", mail.outbox[0].subject)
        self.assertNotIn("\r", mail.outbox[0].subject)
        self.assertEqual(mail.outbox[0].to, ["gift@spu.test"])

    # --- when sending goes wrong -----------------------------------------------------------------------------------

    def test_a_failed_send_is_recorded_kept_and_tried_again_later(self):
        self.turn_on()
        a = self.alert()
        now = timezone.now()
        with mock.patch.object(notify, "_send", side_effect=OSError(f"Authentication failed for {PASSWORD}")):
            record = notify.sweep(now)
        self.assertFalse(record.ok)
        self.assertNotIn(PASSWORD, record.error)                                  # the password never reaches the log
        self.assertIsNone(Alert.objects.get(pk=a.pk).notified_at)
        self.assertIsNone(notify.sweep(now + timedelta(minutes=1)))               # waits before trying again
        self.assertEqual(Notification.objects.count(), 1)
        record = notify.sweep(now + timedelta(minutes=6))
        self.assertTrue(record.ok)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIsNotNone(Alert.objects.get(pk=a.pk).notified_at)

    def test_at_most_six_emails_an_hour_and_the_rest_wait(self):
        self.turn_on()
        now = timezone.now()
        for k in range(notify.MAX_PER_HOUR):
            Notification.objects.create(created_at=now - timedelta(minutes=10 + k), kind=Notification.Kind.ALERTS, subject="s",
                                        recipients="gift@spu.test", alerts=1, ok=True)
        a = self.alert()
        self.assertIsNone(notify.sweep(now))
        self.assertEqual(len(mail.outbox), 0)
        self.assertIsNone(Alert.objects.get(pk=a.pk).notified_at)
        notify.sweep(now + timedelta(minutes=55))                                 # the hour has moved on; still news
        self.assertEqual(len(mail.outbox), 1)

    def test_failed_attempts_and_test_emails_do_not_use_up_the_hourly_limit(self):
        self.turn_on()
        now = timezone.now()
        for k in range(notify.MAX_PER_HOUR):
            Notification.objects.create(created_at=now - timedelta(minutes=30), kind=Notification.Kind.ALERTS, subject="s",
                                        recipients="x", ok=False, error="down")
            Notification.objects.create(created_at=now - timedelta(minutes=30), kind=Notification.Kind.TEST, subject="s",
                                        recipients="x", ok=True)
        self.alert()
        notify.sweep(now)
        self.assertEqual(len(mail.outbox), 1)

    def test_the_worker_step_swallows_every_error(self):
        from core.management.commands.run_worker import Worker
        with mock.patch.object(notify, "sweep", side_effect=RuntimeError("boom")):
            Worker({}).notify()                                                   # must not raise


@override_settings(**READY)
class SettingsTests(TestCase):
    def setUp(self):
        self.admin = make_user("boss", role=User.Role.ADMINISTRATOR)

    def test_addresses_are_split_checked_and_deduplicated(self):
        self.assertEqual(notify.parse_recipients("a@x.test, b@x.test;c@x.test\nA@x.test  d@x.test"),
                         ["a@x.test", "b@x.test", "c@x.test", "d@x.test"])
        self.assertEqual(notify.parse_recipients(""), [])
        with self.assertRaisesMessage(notify.NotifyError, "'not-an-address' is not an email address."):
            notify.parse_recipients("a@x.test, not-an-address")
        with self.assertRaises(notify.NotifyError):
            notify.parse_recipients(", ".join(f"u{i}@x.test" for i in range(notify.MAX_RECIPIENTS + 1)))

    def test_turning_it_on_needs_an_address(self):
        with self.assertRaises(notify.NotifyError):
            notify.save_settings(self.admin, True, "", True)
        self.assertFalse(notify.get_settings()["enabled"])

    def test_turning_it_off_keeps_the_addresses_and_turning_it_on_again_restarts_the_clock(self):
        t0 = timezone.now() - timedelta(hours=2)
        notify.save_settings(self.admin, True, "a@x.test", True, now=t0)
        self.assertEqual(notify.get_settings()["since"], t0)
        notify.save_settings(self.admin, True, "a@x.test, b@x.test", True, now=t0 + timedelta(minutes=30))
        self.assertEqual(notify.get_settings()["since"], t0)                       # still on: the clock keeps running
        notify.save_settings(self.admin, False, "a@x.test, b@x.test", True)
        s = notify.get_settings()
        self.assertEqual((s["enabled"], s["recipients"]), (False, ["a@x.test", "b@x.test"]))
        t1 = timezone.now()
        notify.save_settings(self.admin, True, "a@x.test", True, now=t1)
        self.assertEqual(notify.get_settings()["since"], t1)

    def test_a_test_email_goes_to_the_typed_addresses(self):
        sent, text = notify.send_test("a@x.test, b@x.test")
        self.assertTrue(sent)
        self.assertEqual(mail.outbox[0].to, ["a@x.test", "b@x.test"])
        self.assertIn("a@x.test", text)
        n = Notification.objects.get()
        self.assertEqual((n.kind, n.ok), (Notification.Kind.TEST, True))

    def test_a_test_email_explains_every_way_it_can_not_be_sent(self):
        self.assertEqual(notify.send_test("")[0], False)
        self.assertIn("not an email address", notify.send_test("nope")[1])
        with override_settings(EMAIL_HOST=""):
            self.assertIn(".env", notify.send_test("a@x.test")[1])
        with mock.patch.object(notify, "_send", side_effect=OSError(f"refused {PASSWORD}")):
            sent, text = notify.send_test("a@x.test")
        self.assertFalse(sent)
        self.assertIn("could not be sent", text)
        self.assertNotIn(PASSWORD, text)
        self.assertFalse(Notification.objects.get().ok)
        self.assertEqual(len(mail.outbox), 0)


@override_settings(HPCSENTINEL_CONFIG=EXAMPLE, PROMETHEUS_URL="", **READY)
class ScreenTests(TestCase):
    def setUp(self):
        self.admin = make_user("boss", role=User.Role.ADMINISTRATOR)
        self.client.force_login(self.admin)
        self.url = reverse("controls")

    def test_an_administrator_saves_the_email_settings(self):
        response = self.client.post(self.url, {"action": "notify", "enabled": "on", "recipients": "gift@spu.test", "events": "on"}, follow=True)
        self.assertContains(response, "Email alerts are on. They go to gift@spu.test.")
        s = notify.get_settings()
        self.assertEqual((s["enabled"], s["recipients"], s["events"]), (True, ["gift@spu.test"], True))
        self.assertContains(response, "gift@spu.test</textarea>")                    # shown back as saved

    def test_a_mistake_is_explained_and_nothing_is_saved(self):
        for data, text in (({"enabled": "on", "recipients": "gift@spu.test, nope"}, "'nope' is not an email address."),
                           ({"enabled": "on", "recipients": ""}, "Add at least one email address")):
            response = self.client.post(self.url, {"action": "notify", **data})
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, escape(text))                                  # the page escapes the quotes
        self.assertFalse(PlatformSetting.objects.filter(key__startswith="notify_").exists())

    def test_turning_it_off_keeps_the_addresses(self):
        self.client.post(self.url, {"action": "notify", "enabled": "on", "recipients": "gift@spu.test"})
        response = self.client.post(self.url, {"action": "notify", "recipients": "gift@spu.test"}, follow=True)
        self.assertContains(response, "Email alerts are off.")
        self.assertEqual(notify.get_settings()["recipients"], ["gift@spu.test"])

    def test_the_test_button_sends_to_what_is_typed_without_saving_it(self):
        response = self.client.post(self.url, {"action": "notify_test", "recipients": "gift@spu.test"})
        self.assertContains(response, "Test email sent to gift@spu.test.")
        self.assertContains(response, "gift@spu.test</textarea>")                    # what was typed stays on screen
        self.assertEqual(len(mail.outbox), 1)
        self.assertFalse(PlatformSetting.objects.filter(key__startswith="notify_").exists())
        self.assertContains(self.client.get(self.url), "HPCSentinel test email")      # listed under recent emails

    def test_a_failed_test_shows_the_reason_and_never_the_password(self):
        with mock.patch.object(notify, "_send", side_effect=OSError(f"535 Authentication failed {PASSWORD}")):
            response = self.client.post(self.url, {"action": "notify_test", "recipients": "gift@spu.test"})
        self.assertContains(response, "could not be sent")
        self.assertContains(response, "535 Authentication failed")
        self.assertNotContains(response, PASSWORD)
        self.assertContains(self.client.get(self.url), "Failed")

    def test_only_an_administrator_may_change_it(self):
        self.client.force_login(make_user("op"))
        for action in ("notify", "notify_test"):
            self.assertEqual(self.client.post(self.url, {"action": action, "recipients": "a@x.test"}).status_code, 403)
        self.assertEqual(len(mail.outbox), 0)

    def test_the_page_says_whether_the_server_can_send_and_never_shows_the_password(self):
        page = self.client.get(self.url)
        self.assertContains(page, "Server ready")
        self.assertContains(page, "smtp.test:587 as sentinel@test")
        self.assertNotContains(page, PASSWORD)
        with override_settings(EMAIL_HOST=""):
            page = self.client.get(self.url)
        self.assertContains(page, "Server not set up")
        self.assertContains(page, "EMAIL_")
