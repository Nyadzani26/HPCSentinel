# Requirements status

Updated as the build proceeds. "Done" means implemented and covered by an automated test.
"Backend" means the logic exists and is tested, but there is no web screen for it yet.
"Not started" items that remain Included at submission must be reported in report Section 3.4.

| ID | Requirement (short) | Status | Where |
|---|---|---|---|
| FR-01 | Sign in; deny access otherwise | Done | `hpcsentinel/urls.py`, `core/tests/test_views.py` |
| FR-02 | Two roles, enforced | Backend | `User.role`, `ReviewService`; page-level checks pending |
| FR-03 | Administrator manages users | Partial | Django admin site only |
| FR-04 | Set and test Prometheus address | Partial | Address validated at start-up; no screen |
| FR-05 | Metrics and node mapping from configuration | Done | `core/config.py`, `config/` |
| FR-06 | Readiness report | Not started | |
| FR-07 | Node groups | Model only | `NodeGroup` |
| FR-08 | Retrieve at a configurable interval | Partial | Client and scheduler exist; metric retrieval pending |
| FR-09 | Data-quality checks | Not started | |
| FR-10 | Rates and rolling features | Not started | |
| FR-11 | Load state | Not started | |
| FR-12 | Baselines | Not started | |
| FR-13 | Tree-based model | Not started | |
| FR-14 | Combined scoring | Not started | |
| FR-15 | Persistence rule | Not started | |
| FR-16 | Excluded periods | Model only | `ExcludedPeriod` |
| FR-17 | Scheduled re-learning | Not started | |
| FR-18 | Change threshold and persistence | Partial | In configuration file |
| FR-19 | Alert explanation | Not started | |
| FR-20 to FR-23 | Fleet overview, node detail, alert list, node history | Not started | |
| FR-24 | Alert lifecycle with outcome | Backend | `Alert`, `ReviewService` |
| FR-25 | Notes | Backend | `ReviewService.add_note` |
| FR-26 | Administrator reopens | Backend | `ReviewService.reopen` |
| FR-27 | Record user and time | Backend | `AlertAction`, `ConfigVersion` |
| FR-28 to FR-30 | Replay, experiment log, evaluation report | Not started | |
