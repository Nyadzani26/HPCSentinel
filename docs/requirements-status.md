# Requirements status

"Done" means implemented and covered by an automated test (`python manage.py test`, 103 tests).
"Pending evidence" means implemented, but its verification needs a run on a real cluster
(timings, browsers, a clean-host install) that has not been done yet. Anything marked Included in
the report that is not "Done" or "Pending evidence" must be reported in report Section 3.4.

## Functional requirements (all 30 Included requirements)

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
| FR-10 | Rates and rolling features | Done | `features.py` (rates come from the PromQL in the configuration) | `FeatureTests` |
| FR-11 | Load state from the node's own metrics | Done | `features.feature_table` | `FeatureTests` |
| FR-12 | Baselines per node and load state | Done | `detector.Detector.train` | `PipelineTests` |
| FR-13 | Tree-based model, stored with a version | Done | `detector.py`, `jobs.train`, `ModelVersion` | `test_training_stores_a_model_version` |
| FR-14 | Combined scoring (baseline, peers, model) | Done | `Detector.score_window`, `combine` | `PipelineTests` |
| FR-15 | Alert only after N consecutive abnormal windows | Done | `engine.AlertManager` | `AlertRuleTests` |
| FR-16 | Excluded periods | Done | `ExcludedPeriod`, `jobs._drop_excluded`, controls page | `test_excluded_periods_are_not_learned_from`, `test_excluded_period_is_saved_and_validated` |
| FR-17 | Scheduled re-learning from recent normal data | Done | `jobs.relearn`, `run_worker` | `test_scheduled_relearning_...`, `test_relearning_leaves_out_alerts_...` |
| FR-18 | Change threshold and persistence | Done | controls page, `settings_store` | `test_settings_are_validated_and_saved`, `test_effective_config_applies_saved_settings` |
| FR-19 | Explanation with every alert | Done | `engine.explain`, alert detail page | `test_the_explanation_names_...`, `test_replay_detects_...` |
| FR-20 | Fleet overview | Done | `views.home`, `fleet.html` | `FleetScreenTests` |
| FR-21 | Node detail with metric charts and alert points | Done | `views.node_detail`, `node_detail.html` | `test_node_detail_renders_without_data` (charts are drawn in the browser; checked by screenshot) |
| FR-22 | Alert list filtered by node, status, time range | Done | `views.alert_list` | `AlertFilterTests`, `TriageTests.test_list_filters` |
| FR-23 | Per-node alert history with outcomes | Done | `node_detail.html` | screenshot; history counts in `explain` |
| FR-24 | Open → Acknowledged → Closed with outcome | Done | `Alert`, `ReviewService` | `test_models`, `test_review`, `TriageTests` |
| FR-25 | Notes | Done | `ReviewService.add_note` | `test_review`, `TriageTests` |
| FR-26 | Administrator reopens | Done | `ReviewService.reopen` | `TriageTests` |
| FR-27 | Record user and time of every action and change | Done | `AlertAction`, `ConfigVersion`, `PlatformSetting`, `Job.requested_by` | `test_review`, settings tests |
| FR-28 | Replay through the pipeline in time order | Done | `jobs.replay` | `PipelineTests` |
| FR-29 | Import the experiment log | Done | `jobs.import_experiments`, controls page | `ExperimentImportTests`, `test_experiment_upload` |
| FR-30 | Evaluation report (detection rate, delay, false alerts) | Done | `jobs.evaluate`, controls page | `test_evaluation_reports_event_level_metrics` |

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
| NFR-13 Maintainability | Done | Pure pipeline modules separate from Django; 103 tests |

## Known limitations

* A node is not scored for up to an hour after its load state changes (a 60-minute stability rule), so a fault that begins exactly at a job start is detected later.
* Load state comes from the node's own CPU use, not from the job scheduler (FR-33 deferred).
* Hardware sensors are not used (FR-32 deferred). The platform sends no notifications (FR-31 deferred).
* Assignment of nodes to groups is made in the Django administration site; one default group is used otherwise.
