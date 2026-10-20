# Design notes from testing

## Load-state stability and feature spans (replay of 18 hours, 8 synthetic nodes, two injected faults)

Load state is inferred from CPU use alone, so a change of load changes every feature at once. A window that
straddles a change describes neither regime, and an unprotected detector alerts on every node at every job
start and end. The platform therefore scores a node only after its load state has been steady for a
"stability period", and the period must be at least as long as the longest feature span.

| Feature spans | Stability | Alerts | Result |
|---|---|---|---|
| 5, 15, 60 min | 60 min | 2 (both faults) | correct, but a node is unscored for up to an hour after each load change |
| 5, 15, 60 min | 15 min | 20 (2 faults + 18 false) | false alert on every node at 09:20 and 17:20 |
| 5, 15 min | 15 min | 2 (both faults) | correct, lockout reduced to 15 minutes (adopted default) |

Configuration validation rejects a stability period shorter than the longest span. Slower features (for
example 60 minutes) remain available by setting both values to 60.

## Cold start for rarely loaded nodes

A node needs 30 steady windows of its own in a load state to have a per-node baseline for it. A node with fewer
is compared with its group's pooled baseline for that state (`Detector.group_baselines`), so it is still scored.

## Network metrics

Network queries sum all physical interfaces (excluding loopback, virtual and bridge devices) instead of naming
one interface, so the same configuration works on clusters whose interfaces are not called eth0.

## Practical-significance floor (`min_effect`)

Observed on the SPU Ranger cluster on 20 October 2026: after an idle period the learned spread of `cpu_busy` was
about 0.0002, so an idle node whose CPU rose to 1.1 percent scored the maximum deviation (10) and, with the model and peer
scores, produced an alert. Nobody would act on that. A learned spread is not a measure of what matters.

Each metric can therefore carry a floor in the metric's own unit (`detection.min_effect`): a metric must move by at least
that much before the move counts, whatever its learned spread. The floor is applied to the baseline and the peer
comparison (half the value for the spread features), is stored inside the saved model so a replay uses the floor the model
was trained with, and defaults to none for unlisted metrics. The shipped configurations list conservative values.

Measured on the CHPC synthetic dataset (validation month, August; the test month was not used), with floors chosen from the
metric scales before measuring: false alerts fell from 1.75 to 1.39 per node-day at the same event recall (0.64), and the
median warning before a failure fell from 3.9 to 3.3 hours. Doubling the floors lowered false alerts further to 1.04 but
cost recall (0.57), which is why the defaults are not larger. The gain on that dataset is modest because its main source of
false alerts is different: five normal workload types are folded into one "loaded" state. That is a separate improvement
(learned operating regimes) and is not part of this change.

## Zero-touch learning (replaces the manual train, upload and date-picking workflow)

**Problem.** The first design asked an administrator to pick training dates, name a model, run stress tests to
obtain "normal" data and upload a CSV of injected faults. On the Ranger test cluster this meant a person had to
know when the cluster was healthy before the platform could be useful, and one mistyped year (2027) produced a
model "learned" from a period that had not happened. A tool that needs an expert to feed it is not a tool a
production administrator adopts.

**Decision.** The platform decides for itself when and from what to learn (`core/services/autolearn.py`). The
worker checks every 15 minutes and chooses one of four outcomes: *collecting* (not enough clean data yet; it
reports progress), *learned* (a first model, or a better one because enough new data arrived), *current*
(nothing to do) or *waiting* (it checked or learned recently). The learning window is the last `learn_days`
days (default 14) of reliable data, minus time around alerts nobody dismissed as false alarms and any
maintenance period an administrator marked.

**Self-cleaning.** Real data contains the faults we are trying to detect, and a model fitted on them learns
them as normal. After the first fit the learner scores its own training rows and discards the most extreme ones
(z above 6, at most 5% of any node and load state), then fits again (`core/pipeline/cleaning.py`). The number of
rows removed is stored with the model and shown in the interface.

**Interface.** Settings keeps what only a person can know (where Prometheus is, how sensitive alerts should be)
as three named choices: Relaxed (threshold 4.0, 4 checks), Balanced (3.0, 3; the default) and Sensitive (2.5, 2),
with the raw numbers behind a "Fine-tune" disclosure. A Learning tab states in plain language what has been
learned and how much data it used. The old manual tools (learn from a given period, replay and evaluate, mark
periods to ignore) remain under Advanced because they are needed to *validate* the detector for the dissertation,
but nothing in normal operation depends on them.

**What is deliberately not automated.** The detector never learns from synthetic data. Synthetic and injected
anomalies are used only to test it.

**Trade-offs.** Automatic learning can absorb a slow, genuine drift as normal. The 14-day window bounds how far
that can go, a weekly relearn is the upper limit on staleness, and the cleaning step removes sudden departures
but not gradual ones. This is recorded as a known limitation.
