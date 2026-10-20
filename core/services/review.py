"""Review of alerts (workflow 2, Figures 5 and 6).

Reviewing is optional and one step: a person may record "real fault" or "false alarm" on an alert that is still
active, that has recovered by itself, or on many at once. Acknowledging remains available to say "I am looking".

ReviewService is the only way the web application changes an alert. It checks who is
acting, asks the Alert to make the transition, and writes the AlertAction in the same
transaction (BR-11), so an alert is never changed without a record of who changed it.
"""
from django.db import transaction
from django.utils import timezone

from core.models import Alert, AlertAction, InvalidTransition, User


class PermissionDenied(Exception):
    """Raised when the user is not signed in, is inactive, or lacks the required role."""


class ReviewService:
    # --- helpers -------------------------------------------------------------------------
    @staticmethod
    def _require_user(user):
        if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
            raise PermissionDenied("You must be signed in to review alerts.")

    @staticmethod
    def _record(alert, user, action, outcome=None, note=""):
        return AlertAction.objects.create(alert=alert, user=user, action=action, outcome=outcome, note=note)

    @staticmethod
    def _locked(alert):
        # select_for_update serialises concurrent reviewers on PostgreSQL.
        return Alert.objects.select_for_update().get(pk=alert.pk)

    # --- operations (UC-03, UC-04) ---------------------------------------------------------
    def acknowledge(self, alert, user, note=""):
        self._require_user(user)
        with transaction.atomic():
            alert = self._locked(alert)
            alert.acknowledge()
            self._record(alert, user, AlertAction.Action.ACKNOWLEDGE, note=note)
        return alert

    def close(self, alert, user, outcome, note=""):
        self._require_user(user)
        with transaction.atomic():
            alert = self._locked(alert)
            alert.close(outcome, timezone.now())
            self._record(alert, user, AlertAction.Action.CLOSE, outcome=outcome, note=note)
        return alert

    def close_many(self, alert_ids, user, outcome, note=""):
        """Review several alerts at once with the same outcome. Alerts already reviewed are skipped; returns how many changed."""
        self._require_user(user)
        changed = 0
        with transaction.atomic():
            for alert in Alert.objects.select_for_update().filter(pk__in=list(alert_ids), mode=Alert.Mode.LIVE) \
                    .exclude(status=Alert.Status.CLOSED):
                alert.close(outcome, timezone.now())
                self._record(alert, user, AlertAction.Action.CLOSE, outcome=outcome, note=note)
                changed += 1
        return changed

    def reopen(self, alert, user, note=""):
        self._require_user(user)
        if not user.is_administrator:
            raise PermissionDenied("Only an Administrator can reopen a closed alert.")
        with transaction.atomic():
            alert = self._locked(alert)
            alert.reopen()
            self._record(alert, user, AlertAction.Action.REOPEN, note=note)
        return alert

    def add_note(self, alert, user, note):
        self._require_user(user)
        if not note or not note.strip():
            raise InvalidTransition("A note cannot be empty.")
        with transaction.atomic():
            alert = self._locked(alert)
            if alert.status == Alert.Status.CLOSED:
                # BR-07: a closed alert is changed only after an Administrator reopens it.
                raise InvalidTransition("A closed alert cannot be changed; an Administrator must reopen it first.")
            return self._record(alert, user, AlertAction.Action.NOTE, note=note.strip())
