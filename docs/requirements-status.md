# Requirements status

"Done" means implemented and covered by an automated test (`python manage.py test`, 216 tests).
"Pending evidence" means implemented, but its verification needs a run on a real cluster
(timings, browsers, a clean-host install) that has not been done yet. Anything marked Included in
the report that is not "Done" or "Pending evidence" must be reported in report Section 3.4.

## Functional requirements (all 33 Included requirements)

| ID | Requirement (short) | Status | Where | Tests |
|---|---|---|---|---|
| FR-01 | Sign in; deny access otherwise | Done | `hpcsentinel/urls.py`, `core/views.py` | `test_views`, `test_screens.test_alert_pages_require_sign_in` |
| FR-02 | Two roles, enforced | Done | `User.role`, `administrator_required`, `ReviewService` | `test_screens` (operator refused on controls, users, reopen) |
| FR-03 | Administrator manages users | Done | `views.users`, `user_update` | `UserManagementTests` |
| FR-04 | Set and test the Prometheus address | Done | `views.controls`, `services/settings_store.py` | `test_settings_are_validated_and_saved`, `test_connection_test_reports_failure` |
| FR-05 | Metrics and node mapping from configuration | Done | `core/config.py`, `config/` | `test_config` |
| FR-06 | Readiness report | Done | `services/readiness.py` | `test_readiness_report` |
| FR-07 | Node groups; peers compared within a group | Done | `NodeGroup`, `engine.score_rows` (groups the peer comparison) | `test_replay_detects_...` (default group); group assignment is through the admin site |
| FR-08 | Retrieve at a configurable interval, read-only | Done | `pipeline/cycle.py`, `services/prometheus.py`, `run_worker` | `test_prometheus`, `test_cycle` |
| FR-09 | Data-quality checks (gaps, staleness, identical readings) | Done | `features.feature_table` (gaps, staleness, transitions); `readiness.check` (identical readings) | `FeatureTests`, `test_readiness_report` |
| FR-10 | Rates and rolling features | Done | `features.py`: means over 5 and 15 minutes and standard deviation over 15 minutes (spans configurable); rates come from the PromQL in the configuration | `FeatureTests` |
| FR-11 | Load state from the node's own metrics | Done | `features.feature_table` | `FeatureTests` |
| FR-12 | Baselines per node and load state | Done | `detector.Detector.train` | `PipelineTests` |
| FR-13 | Tree-based model, stored with a version | Done | `detector.py`, `jobs.train`, `ModelVersion` | `test_training_stores_a_model_version` |
| FR-14 | Combined scoring (baseline, peers, model) | Done | `Detector.score_window`, `combine` | `PipelineTests` |
| FR-15 | Alert only after N consecutive abnormal windows | Done | `engine.AlertManager` | `AlertRuleTests` |
| FR-16 | Excluded periods | Done | `ExcludedPeriod`, `jobs._drop_excluded`, controls page | `test_excluded_periods_are_not_learned_from`, `test_excluded_period_is_saved_and_validated` |
| FR-17 | Scheduled re-learning from recent normal data | Done (extended: zero-touch, see below) | `services/autolearn.py`, `jobs.relearn`, `run_worker` | `test_autolearn`, `test_scheduled_relearning_...`, `test_relearning_leaves_out_alerts_...` |
| FR-18 | Change threshold and persistence | Done | controls page, `settings_store` | `test_settings_are_validated_and_saved`, `test_effective_config_applies_saved_settings` |
| FR-19 | Explanation with every alert | Done | `engine.explain`, alert detail page | `test_the_explanation_names_...`, `test_replay_detects_...` |
| FR-20 | Fleet overview | Done | `views.home`, `fleet.html` | `FleetScreenTests` |
| FR-21 | Node detail with metric charts and alert points | Done | `views.node_detail`, `node_detail.html` | `test_node_detail_renders_without_data` (charts are drawn in the browser; checked by screenshot) |
| FR-22 | Alert list filtered by node, status, time range | Done | `views.alert_list` | `AlertFilterTests`, `TriageTests.test_list_filters` |
| FR-23 | Per-node alert history with outcomes | Done | `node_detail.html` | screenshot; history counts in `explain` |
| FR-24 | Alert lifecycle: Active → Recovered (by the platform) → Reviewed with outcome (by a person, optional) | Done (changed, see below) | `Alert`, `ReviewService`, `engine.AlertManager` | `test_models`, `test_review`, `TriageTests`, `AlertLifecycleTests` |
| FR-25 | Notes | Done | `ReviewService.add_note` | `test_review`, `TriageTests` |
| FR-26 | Administrator reopens | Done | `ReviewService.reopen` | `TriageTests` |
| FR-27 | Record user and time of every action and change | Done | `AlertAction`, `ConfigVersion`, `PlatformSetting`, `Job.requested_by` | `test_review`, settings tests |
| FR-28 | Replay through the pipeline in time order | Done | `jobs.replay` | `PipelineTests` |
| FR-29 | Import the experiment log | Done | `jobs.import_experiments`, controls page | `ExperimentImportTests`, `test_experiment_upload` |
| FR-30 | Evaluation report (detection rate, delay, false alerts) | Done | `jobs.evaluate`, controls page | `test_evaluation_reports_event_level_metrics` |
| FR-39 | End an alert by itself when the node is normal again or has stopped reporting; continue the same alert, counting a recurrence, if it returns within the merge period | Done | `engine.AlertManager.settle`, `Alert.resolve`, `Alert.flare_up` | `AlertLifecycleTests`, `TriageTests` |
| FR-40 | Write daily, weekly and monthly reports by itself, save each as it stood, allow download | Done | `services/reports.py`, `Report`, `run_worker.Worker.reports` | `test_reports` (`PeriodTests`, `BuildTests`, `GenerationTests`, `ReportScreenTests`, `WorkerReportTests`) |
| FR-41 | Record one cluster-wide event, not one alert per node, when many nodes become abnormal in the same cycle | Done (thresholds not tuned: one real example so far) | `services/events.py`, `ClusterEvent`, `engine.apply_alert_rules` | `test_events` (`EventRuleTests`, `ClusterCycleTests`, `EventScreenTests`, `EventReportTests`) |

## Changes made after the first deployment on Ranger

The first design (workflows and business rules written before any real cluster was connected) turned out to ask too
much of an administrator. Running it on Ranger showed three problems, and each change below answers one of them.
Every change is recorded here so the report can state it as a deliberate deviation rather than leave it to be found.

| Change | Why | Requirement or rule affected |
|---|---|---|
| Learning is automatic (`services/autolearn.py`). The administrator no longer picks training dates, runs stress tests to obtain "normal" data or uploads a file. The old manual tools remain under Settings, Advanced, for validating the detector. | The platform was useless until an expert fed it; one mistyped year produced a model "learned" from a period that had not happened. | FR-17 extended; FR-16, FR-28, FR-29 become optional tools. |
| Learning data cleans itself (`pipeline/cleaning.py`): the most extreme windows are dropped before fitting. | Real data contains the faults we want to detect; a model fitted on them learns them as normal. | New. |
| An alert is one story: it updates in place while the node stays unusual, ends by itself when the node has been normal for 5 checks or has stopped reporting for 30 minutes, and is continued, not duplicated, if the node flares up within 60 minutes. | An administrator who looks hours later should see one current alert, not a stack of stale ones to delete. Switching Ranger off at night had produced 47 alerts at once. | FR-24 changed; BR-08 now counts only Active alerts. |
| Reviewing is optional and one step. **BR-05 is relaxed:** an alert no longer has to be acknowledged before it can be closed. Acknowledge remains as "I'm looking into it". The platform never records an outcome itself, so Confirmed and False alarm remain human judgements. | An alert that has recovered by itself should not need to be "acknowledged" before it can be judged. | BR-05 relaxed; BR-06 (outcome if and only if Closed) and BR-07 unchanged. |
| Alert states are named Active, Being looked at, Recovered and Reviewed. | Plain language for administrators; `OPEN`, `ACKNOWLEDGED`, `RESOLVED`, `CLOSED` in the database. | None. |
| Many nodes unusual at once are recorded as one cluster-wide event and new per-node alerts are held back for its duration (`services/events.py`). | Switching Ranger off produced 47 alerts at once. | New. Not tuned on labelled incidents; one real example so far. |
| Daily, weekly and monthly reports are written by the worker, with a printable view and CSV downloads (`services/reports.py`). | Administrators and managers need a summary without reading an alert list. | New. |
| Replay does not end or merge alerts; it keeps one alert per abnormal run. | Keeps evaluation reproducible and conservative: false-alert counts measured in replay are at least as high as those the live platform would show. | D-5 refined. |

## Non-functional requirements

| ID | Status | Evidence |
|---|---|---|
| NFR-01 Authorisation | Done | Role checks on every state-changing view; `test_screens` |
| NFR-02 Credentials | Done | Passwords hashed by Django; minimum length 10; secrets only in `.env`; `.env` is git-ignored |
| NFR-03 Input validation | Done | Django forms and model constraints; invalid periods, outcomes, CSV rows rejected (tests) |
| NFR-04 Sessions | Done | HttpOnly, SameSite, idle expiry; `test_views` |
| NFR-05 Non-intrusive | Done | Client issues only HTTP GET to query endpoints; `test_prometheus` |
| NFR-06 Interface speed | Pending evidence | Needs timing on Ranger (Section 7) |
| NFR-07 Detection cycle time | Pending evidence | Needs timing of a 48-node cycle on Ranger (Section 7) |
| NFR-08 Reliability | Done | Outage recorded, no alerts raised, failed jobs recorded; `test_cycle`, `JobRunnerTests` |
| NFR-09 Portability | Pending evidence | Docker Compose; clean-host install to be run and recorded |
| NFR-10 Configurability | Done | Metrics, mapping and detector settings from configuration; `test_config` |
| NFR-11 Reproducibility | Done | Fixed seeds; `test_two_replays_of_the_same_period_give_the_same_alerts` |
| NFR-12 Compatibility | Pending evidence | Browser check to be recorded |
| NFR-13 Maintainability | Done | Pure pipeline modules separate from Django; 216 tests |

## Known limitations

* A node is not scored for up to 15 minutes after its load state changes (a stability rule, 15 minutes by default, equal to the longest feature span), so a fault that begins exactly at a job start is detected later. Because load state comes from the node's own readings (CPU use, plus disk traffic where `load_busy_disk_ratio` is set, as on Ranger), a fault that itself drives CPU above 10% on an idle node (for example a CPU hog) looks like a legitimate job and cannot be told apart without scheduler data (FR-33, deferred). A job that is quiet on every watched signal is read as idle for the same reason.
* Load state comes from the node's own readings (CPU, optionally disk or network), not from the job scheduler (FR-33 deferred).
* Hardware sensors are not used (FR-32 deferred). The platform sends no notifications (FR-31 deferred).
* Assignment of nodes to groups is made in the Django administration site; one default group is used otherwise.
