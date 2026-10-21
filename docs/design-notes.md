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

## A light job that looks idle (`load_idle_cpu_busy`)

On the first live day on Ranger, the input/output-oriented baseline job (profile `io`) left the processor at about
6.5%, under the default 10% idle threshold. Judged on CPU alone its nodes were classed IDLE, looked unusual beside the
idle baseline (scores of 3.6 to 7) and joined a cluster-wide event.

Two remedies were tried and dropped. Counting a busy disk as busy could not work: the compute nodes have no disks (the root file system is NFS from the head
node and is read-only, `/tmp` is memory), and `disk_busy_ratio` stayed at 0.0% on the job's node for the whole run. The
job's file writes never took place at all: it wrote to `/var/tmp`, which is read-only on these nodes, and the writer died
silently while the rest of the job carried on. The same mistake made the first two injected I/O faults run for zero seconds.
The tools now write to a writable folder (the shared home folder for the I/O fault, memory for the baseline profile), stop
with a clear message when they cannot write, and the fault script logs a fault only if it really ran.
Lowering the threshold to 4% classed the job correctly (idle nodes were seen at no more than 0.8% CPU),
but it would have hidden the faults the platform is meant to find: a one-process fault such as the memory or I/O fault
keeps one core busy, which is a few per cent of the node, so with a 4% line the node turns LOADED when the fault starts and
is not scored for the stability period, which is as long as the fault.

The threshold therefore stays at the default 10%, and the cause is treated as a flaw of the test workload: the `io`
profile was lighter than any real job and now uses enough processor to read as loaded. The limit that remains is
inherent to a CPU-only load state: a one-core job, which is a few per cent of a node, is read as idle and scored against
idle behaviour. Telling it apart from a fault needs scheduler data (FR-33, deferred).

## Cluster-wide shared-filesystem bursts

On the same day every node showed bursts of NFS requests (about 30 to 48 per second against a normal 0.6), each
lasting several minutes. A 5-minute burst keeps the 5- and 15-minute features abnormal for about 25 minutes, so every
idle node scored unusual at once and the platform recorded one cluster-wide event (36 of 36 reporting nodes at its
peak) instead of 36 alerts. The cause of the bursts was not established.

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

## Alerts as one story (update in place, recover, merge)

**Problem.** The first lifecycle treated every alert as an event someone must handle: Open, then Acknowledged, then
Closed with an outcome. On Ranger that failed in a predictable way. Someone returning hours later found a stack of
stale alerts that had to be acknowledged before they could be closed, and a single cause (the cluster being switched
off) produced 47 alerts that had to be closed one by one. A tool that creates work in proportion to how often
the world changes is not one an administrator keeps using.

**Decision.** An alert follows one problem on one node (`engine.AlertManager`).

1. *Update in place.* While the node stays abnormal the alert records the latest check (`last_seen_at`), the
   peak and the latest contributing metrics (`explanation["latest"]`). How it looked when it opened is kept.
2. *End by itself.* After `recovery_windows` (5, so 10 minutes) consecutive normal checks, or after
   `no_data_minutes` (30) with no reliable data at all, the alert becomes Recovered. A hard limit of six hours
   means no alert is ever left active indefinitely. The platform records that it did this, as the user "system".
3. *Continue, don't duplicate.* If the node becomes abnormal again within `merge_minutes` (60) of recovering,
   and a person has not yet reviewed the alert, the same alert is continued and `recurrences` increases. A
   reviewed alert is never reopened by the platform; a fresh problem then opens a new alert.
4. *Review is optional and one step.* A person may mark an alert Real fault or False alarm from the list, in
   bulk, or from the alert page, at any time and without acknowledging it first.

**Why humans still decide the outcome.** "Real fault" and "False alarm" are the ground truth for the accuracy
figures in the report. If the platform labelled its own alerts, those figures would measure nothing. Auto-recovery
therefore ends an alert but never judges it. Alerts nobody reviews are counted as unreviewed in reports, not
assumed to be correct or incorrect.

**Why persistence still applies to a returning alert.** A returning node needs the same N consecutive abnormal
windows before the alert is continued, so one noisy check does not reopen it.

**Trade-offs.** Merging can hide that a fault repeatedly came and went; `recurrences` and the timeline keep that
visible. The five-check recovery delay means an alert can stay active about ten minutes longer than the problem;
this is the price of not flapping. Both numbers are configuration, not code.

## Reports (daily, weekly, monthly)

**Why.** An administrator, or the head of department who never opens the platform, needs to answer "how did the
cluster do this week, and can I trust the alerts?" without reading an alert list. Reports are written
automatically, so nobody has to remember to make them.

**How.** `core/services/reports.py` computes a report from what the platform has already stored and never touches
Prometheus. Periods are whole UTC days, Monday-to-Monday weeks and calendar months. The worker calls
`generate_due` every hour and at start-up. It writes every completed period that has data and no report, so a
stopped worker catches up and nothing is duplicated (one report per kind and period is a database constraint).
A report is stored as frozen numbers, so it says what was known at the time. An Administrator can rebuild it to
include reviews made later. "Today so far", "This week so far" and "This month so far" build a live report that
is not saved.

**What is reported.** Alerts raised and how they ended; real faults and false alarms among those a person reviewed;
alerts still waiting for review; the typical and longest alert; the nodes with most alerts; the main cause of each
alert; whether the platform could see the cluster (hours with data, node-hours scored, Prometheus outages); and
the models learned. Each report opens with a short plain-language summary and can be printed or saved as a PDF
from the browser, and downloaded as CSV (alerts and nodes).

**Honesty rules.** The real-fault rate is computed only over alerts a person reviewed, and the summary says
how many are still waiting, because treating unreviewed alerts as correct or incorrect would make the figure
meaningless. For a cluster that is switched off overnight, "reliable data" is naturally low; the report says
so instead of presenting it as a fault. Durations of alerts still active are measured to the end of the period,
not to the wall clock, so a report does not change when it is opened later.

**CSV safety.** Cells that begin with `=`, `+`, `-` or `@` are prefixed so a spreadsheet cannot run them as formulas.

## Cluster-wide events (one event instead of forty-seven alerts)

**Problem.** Switching Ranger off at 16:29 made 47 nodes change at the same moment and produced 47 alerts. The
administrator then had to close them one at a time, and the real signal (one shared cause) was buried. The same
would happen with a power or cooling fault, a network switch failing, or a large job starting on every node.

**Decision.** In each cycle `engine.apply_alert_rules` counts how many nodes with usable data are abnormal. If at
least `cluster_event_min_nodes` (5) and at least `cluster_event_fraction` (30%) of them are abnormal together,
`services/events.py` records one `ClusterEvent` (when it started, the most nodes involved, which nodes, and the
main contributing metric) and holds back the opening of new per-node alerts for as long as the event lasts.
The event ends after three consecutive ordinary cycles.

**What is not hidden.** A node that already has an alert keeps updating it. Windows are stored exactly as before,
so a node that is still abnormal when the event ends meets the persistence rule at once and raises its own alert.
A genuine fault underneath an event is therefore delayed, never lost.

**Why a share and a minimum.** Independent faults rarely strike 30% of a cluster in the same two minutes, so the
share separates a shared cause from a coincidence. The minimum stops two nodes of a four-node test cluster being
called an event. Both are configuration; they have not been tuned on a labelled cluster-wide incident, and the
Ranger shutdown is the only real example so far.

**Known limitation.** The rule detects that many nodes changed together, not why. A planned shutdown and a power
failure look the same to it. The event page says what changed and leaves the interpretation to the administrator.

## Interface redesign (22 Oct 2026)

**Problem.** The first interface was correct but flat: a grey sidebar, identical white cards and a wall of 48 tiles.
After the 21 Oct campaign the Overview also had a real weakness: it showed only the present moment. A person who
had been away for a day could not see when things changed, which nodes were involved, or whether a burst was one
shared event or many separate faults, without opening several pages.

**Decision.** The design rule is that colour is reserved for state. Everything else is ink on paper. The five
state colours each mean one thing: green normal, red unusual, amber under review, indigo part of a cluster-wide
event, grey not scored. They are used the same way in the tiles, pills, chart bands and the history map.

- **The history map.** The Overview now has a node-by-time map of the last 24 hours (`services/timeline.py`,
  `static/core/watch.js`): one row per node, one column per 15 minutes, drawn on a canvas. A cell is unusual when any
  scored check in it reached the alert level, and a cluster-event cell when that node belongs to an event covering
  that column. Cells with no data are drawn as dots so an empty stretch (the cluster was switched off) reads as
  "nothing was recorded" and not as "everything was fine". Each row and each event band links to its page. The node
  tiles below remain the accessible, text version of the same information.
- **Top bar instead of a sidebar.** More width for the map and tables. The bar has a 3 px edge that is green,
  red, indigo or amber for the overall state, so the state is visible from any page without reading anything.
- **Past events are one line.** Only an event that is still going gets a full notice. Ended events collapse into a
  single expandable row, because three tall notices pushed the map below the fold.
- **Tiles.** Only exceptions carry colour (a filled tile and a coloured edge); a normal node has a small green
  dot. Forty-three identical green tiles hid the two that mattered.
- **Counting.** Nodes held back by a running event are counted as "In cluster event", not as "Need attention".
  The first redesign counted them twice and showed "2 nodes need attention" beside a tally of 24.
- **Type.** Bricolage Grotesque for headings and the large numbers, Public Sans for text. Both are bundled
  (`static/core/fonts`, OFL licences included), so the page needs no external font service on a cluster network.
- **Motion.** One reveal of the map when the Overview opens. Reduced motion is respected. Light and dark follow
  the system setting. Everything works down to 390 px wide; on a phone the map scrolls and opens at "now".

**Not done on purpose.** The map shows the combined score only, not the three components. Those are on the node
and alert pages, where there is room to explain them.

**Two defects found by looking at screenshots rather than reading the CSS.** The progress-bar class `.bar`
collided with the top bar's class and collapsed the whole header to half a rem, and the first draft of the map hid
the empty part of the day, which made a half-day of data look like a mostly empty chart. Both were invisible in
the tests.

### Second pass: the map above the fold

The first version put the notices and alert cards above the map, so on a 900 px screen the best part of the Overview
was off the bottom. The Overview is now two columns: the map on the left and a side panel on the right with, in
order of urgency, an event that is still going, the alerts that need attention, a one-line summary of past events, and
the review reminder. On a narrow screen the panel moves above the map. When there is nothing to show the panel is
left out and the map takes the full width.

- The map rows are 10 px with 2 px between them, close to square cells, and the node names stay legible.
- When most of the day has no data the map says "Nothing recorded before 13:45 UTC" instead of leaving a field of dots
  unexplained.
- On a phone the map shows the last 12 hours, not 24, because 96 columns become hairlines at 320 px, and a scrolled
  canvas hides the node names.
- The alert page now opens with a score card (highest score, and the score over the life of the alert against the
  alert level). That chart used to sit unlabeled at the bottom of "Why this was flagged".

## Experiment log: intensity text

`ExperimentEntry.intensity` was limited to 20 characters. SQLite, used in the tests, does not enforce that limit
and PostgreSQL does, so the 21 Oct log (which described each fault in a sentence) imported in the tests and failed
with a server error in production. The limit is now 200 characters, and an over-long value is refused with a message
naming the row, so the file is never half-imported and the person is told what to shorten.
