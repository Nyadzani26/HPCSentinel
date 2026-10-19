"""Database constraints that carry business rules (Section 5.6.4)."""
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from core.models import Alert, CycleRun, ExcludedPeriod, ExperimentEntry, NodeWindow, User
from core.tests.helpers import make_alert, make_config, make_node, make_user


class AlertConstraintTests(TestCase):
    def test_open_alert_cannot_have_an_outcome(self):  # BR-06
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_alert(status=Alert.Status.OPEN, outcome=Alert.Outcome.CONFIRMED)

    def test_closed_alert_must_have_an_outcome(self):  # BR-06
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_alert(status=Alert.Status.CLOSED, outcome=None)

    def test_closed_alert_with_outcome_is_accepted(self):
        alert = make_alert(status=Alert.Status.CLOSED, outcome=Alert.Outcome.FALSE_ALARM)
        self.assertEqual(alert.status, Alert.Status.CLOSED)

    def test_only_one_live_open_alert_per_node(self):  # BR-08
        config = make_config()
        node = make_node()
        make_alert(node=node, config=config)
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_alert(node=node, config=config, status=Alert.Status.ACKNOWLEDGED)

    def test_new_alert_allowed_once_previous_is_closed(self):  # BR-08
        config = make_config()
        node = make_node()
        make_alert(node=node, config=config, status=Alert.Status.CLOSED, outcome=Alert.Outcome.CONFIRMED)
        make_alert(node=node, config=config)

    def test_replay_alerts_do_not_clash_with_live_alerts(self):
        config = make_config()
        node = make_node()
        make_alert(node=node, config=config)
        make_alert(node=node, config=config, mode=Alert.Mode.REPLAY)


class OtherConstraintTests(TestCase):
    def test_node_is_scored_once_per_cycle(self):
        config = make_config()
        node = make_node()
        cycle = CycleRun.objects.create(started_at=timezone.now(), source_reachable=True, config=config)
        NodeWindow.objects.create(node=node, cycle=cycle, window_end=timezone.now(), quality="RELIABLE")
        with self.assertRaises(IntegrityError), transaction.atomic():
            NodeWindow.objects.create(node=node, cycle=cycle, window_end=timezone.now(), quality="RELIABLE")

    def test_excluded_period_must_end_after_it_starts(self):
        admin = make_user("admin", User.Role.ADMINISTRATOR)
        now = timezone.now()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ExcludedPeriod.objects.create(start_at=now, end_at=now - timedelta(hours=1), reason="bad", created_by=admin)

    def test_experiment_entry_must_end_after_it_starts(self):
        admin = make_user("admin", User.Role.ADMINISTRATOR)
        node = make_node()
        now = timezone.now()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ExperimentEntry.objects.create(
                node=node, anomaly_type="CPU", start_at=now, end_at=now, imported_by=admin
            )

    def test_administrator_property(self):
        self.assertTrue(make_user("a", User.Role.ADMINISTRATOR).is_administrator)
        self.assertFalse(make_user("o", User.Role.OPERATOR).is_administrator)
        self.assertFalse(make_user("d", User.Role.ADMINISTRATOR, active=False).is_administrator)

    def test_superuser_is_an_administrator(self):
        user = User.objects.create_superuser("root", password="correct-horse-battery")
        self.assertEqual(user.role, User.Role.ADMINISTRATOR)
        self.assertTrue(user.is_administrator)
