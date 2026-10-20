"""Workflow 2: reviewing an alert (Figures 5, 6 and 7; BR-05, BR-06, BR-07, BR-11)."""
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.test import TestCase

from core.models import Alert, AlertAction, InvalidTransition, User
from core.services.review import PermissionDenied, ReviewService
from core.tests.helpers import make_alert, make_node, make_user


class ReviewWorkflowTests(TestCase):
    def setUp(self):
        self.service = ReviewService()
        self.operator = make_user("operator")
        self.admin = make_user("admin", User.Role.ADMINISTRATOR)

    def test_acknowledge_records_who_and_when(self):
        alert = make_alert()
        self.service.acknowledge(alert, self.operator)
        alert.refresh_from_db()
        self.assertEqual(alert.status, Alert.Status.ACKNOWLEDGED)
        action = alert.actions.get()
        self.assertEqual((action.action, action.user), (AlertAction.Action.ACKNOWLEDGE, self.operator))

    def test_an_active_alert_can_be_reviewed_without_acknowledging_first(self):  # BR-05 relaxed
        alert = make_alert()
        self.service.close(alert, self.operator, Alert.Outcome.CONFIRMED)
        alert.refresh_from_db()
        self.assertEqual((alert.status, alert.outcome), (Alert.Status.CLOSED, Alert.Outcome.CONFIRMED))

    def test_a_recovered_alert_can_be_reviewed_in_one_step(self):
        alert = make_alert(status=Alert.Status.RESOLVED)
        self.service.close(alert, self.operator, Alert.Outcome.FALSE_ALARM)
        alert.refresh_from_db()
        self.assertEqual((alert.status, alert.outcome), (Alert.Status.CLOSED, Alert.Outcome.FALSE_ALARM))
        self.assertEqual(alert.actions.get().user, self.operator)

    def test_a_reviewed_alert_cannot_be_reviewed_again(self):  # BR-07
        alert = make_alert(status=Alert.Status.CLOSED, outcome=Alert.Outcome.CONFIRMED)
        with self.assertRaises(InvalidTransition):
            self.service.close(alert, self.operator, Alert.Outcome.FALSE_ALARM)
        alert.refresh_from_db()
        self.assertEqual(alert.outcome, Alert.Outcome.CONFIRMED)

    def test_a_recovered_alert_cannot_be_acknowledged(self):
        with self.assertRaises(InvalidTransition):
            self.service.acknowledge(make_alert(status=Alert.Status.RESOLVED), self.operator)

    def test_review_many_at_once(self):
        a = make_alert(make_node("compute01"), status=Alert.Status.RESOLVED)
        b = make_alert(make_node("compute02"))
        done = make_alert(make_node("compute03"), status=Alert.Status.CLOSED, outcome=Alert.Outcome.CONFIRMED)
        changed = self.service.close_many([a.pk, b.pk, done.pk], self.operator, Alert.Outcome.FALSE_ALARM, note="stress test")
        self.assertEqual(changed, 2)
        for alert in (a, b):
            alert.refresh_from_db()
            self.assertEqual((alert.status, alert.outcome), (Alert.Status.CLOSED, Alert.Outcome.FALSE_ALARM))
            self.assertEqual(alert.actions.get().note, "stress test")
        done.refresh_from_db()
        self.assertEqual(done.outcome, Alert.Outcome.CONFIRMED)   # an earlier judgement is never overwritten

    def test_closing_requires_an_outcome(self):  # BR-06
        alert = make_alert(status=Alert.Status.ACKNOWLEDGED)
        for bad in (None, "", "MAYBE"):
            with self.assertRaises(InvalidTransition):
                self.service.close(alert, self.operator, bad)
        alert.refresh_from_db()
        self.assertEqual(alert.status, Alert.Status.ACKNOWLEDGED)

    def test_close_with_outcome(self):
        alert = make_alert(status=Alert.Status.ACKNOWLEDGED)
        self.service.close(alert, self.operator, Alert.Outcome.FALSE_ALARM, note="new workload")
        alert.refresh_from_db()
        self.assertEqual((alert.status, alert.outcome), (Alert.Status.CLOSED, Alert.Outcome.FALSE_ALARM))
        self.assertIsNotNone(alert.closed_at)
        action = alert.actions.get()
        self.assertEqual((action.action, action.outcome, action.note), (AlertAction.Action.CLOSE, "FALSE_ALARM", "new workload"))

    def test_operator_cannot_reopen(self):  # BR-04, BR-07
        alert = make_alert(status=Alert.Status.CLOSED, outcome=Alert.Outcome.CONFIRMED)
        with self.assertRaises(PermissionDenied):
            self.service.reopen(alert, self.operator)
        alert.refresh_from_db()
        self.assertEqual(alert.status, Alert.Status.CLOSED)

    def test_administrator_reopens_to_acknowledged(self):  # BR-07
        alert = make_alert(status=Alert.Status.CLOSED, outcome=Alert.Outcome.CONFIRMED)
        self.service.reopen(alert, self.admin)
        alert.refresh_from_db()
        self.assertEqual(alert.status, Alert.Status.ACKNOWLEDGED)
        self.assertIsNone(alert.outcome)
        self.assertIsNone(alert.closed_at)
        self.assertEqual(alert.actions.get().action, AlertAction.Action.REOPEN)

    def test_closed_alert_cannot_receive_a_note(self):  # BR-07
        alert = make_alert(status=Alert.Status.CLOSED, outcome=Alert.Outcome.CONFIRMED)
        with self.assertRaises(InvalidTransition):
            self.service.add_note(alert, self.admin, "late thought")

    def test_note_on_open_alert(self):
        alert = make_alert()
        self.service.add_note(alert, self.operator, "  looks like the new job  ")
        self.assertEqual(alert.actions.get().note, "looks like the new job")

    def test_empty_note_is_rejected(self):
        with self.assertRaises(InvalidTransition):
            self.service.add_note(make_alert(), self.operator, "   ")

    def test_anonymous_and_inactive_users_are_refused(self):
        alert = make_alert()
        with self.assertRaises(PermissionDenied):
            self.service.acknowledge(alert, AnonymousUser())
        inactive = make_user("gone", active=False)
        with self.assertRaises(PermissionDenied):
            self.service.acknowledge(alert, inactive)

    def test_status_change_and_audit_record_are_one_transaction(self):  # Figure 6, BR-11
        alert = make_alert()
        with mock.patch.object(ReviewService, "_record", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                self.service.acknowledge(alert, self.operator)
        alert.refresh_from_db()
        self.assertEqual(alert.status, Alert.Status.OPEN)
        self.assertEqual(AlertAction.objects.count(), 0)
