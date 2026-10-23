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
Prometheus. Periods are whole days, Monday-to-Monday weeks and calendar months in the display time zone (see
"Time zone" below). The worker calls
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
  *Replaced in the fourth pass below by one family, Inter Tight.*
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
- When most of the day has no data the map says "Nothing recorded before 13:45 SAST" instead of leaving a field of dots
  unexplained.
- On a phone the map shows the last 12 hours, not 24, because 96 columns become hairlines at 320 px, and a scrolled
  canvas hides the node names.
- The alert page now opens with a score card (highest score, and the score over the life of the alert against the
  alert level). That chart used to sit unlabeled at the bottom of "Why this was flagged".

### Third pass: everything is a panel

After the second pass the Overview still had loose parts: headings and filters floating on the page background, notices
of different shapes, and a flat list of 48 tiles. One rule now covers every page: a block is a card, and a card
that has a title has a tinted title band.

- The page header, the status headline, the progress strip, the settings tabs and the report figures are cards. The
  status headline carries the state on its left edge (red, green or grey).
- The side panel is three panels: Needs attention (rows with a count), Cluster-wide events (a running event on top, the
  earlier ones folded away) and Recovered alerts.
- The map's legend moved to a footer strip, and the node list became a panel with its search and filters in a bar
  and its footnote in a footer. The list is split by chassis, as the map is (see "Chassis layout" below), so a chassis reads the same in both. A
  chassis is hidden while a search matches none of its nodes.
- Alerts is one panel: filters, bulk actions and the table share a single card.
- Headings that used to float above a table now sit inside it (Settings, Users, node history).
- Spacing comes from one value (`--gap`) applied to the children of `main`, not from per-page margins.
- On a phone the node list shows two tiles per row, and the alerts list is one block per alert instead of a table row.
  A wide table (the per-node report table, say) scrolls inside its card, so no page is wider than the screen. A script
  (a throwaway script, not in the repository) measured every page at 390 and 768 px for this.

**Stale styles after a deploy.** The stylesheet and script are served by WhiteNoise under fixed names with
`Cache-Control: max-age=60`, so a browser or a proxy in front can keep an old copy after an update, and new markup
against old CSS looks unorganised. Their addresses now carry a fingerprint of their content
(`sentinel.css?v=abc1234` and `watch.js?v=abc1234`, `core/context.py`), which changes whenever either file does, and the same value is shown
in the footer ("interface abc1234") so a person can tell which version they are looking at.

### Fourth pass: list and detail, a Nodes tab (22 Oct 2026, afternoon)

**Why.** The third pass was consistent but still looked like a template: soft cards, two display fonts, a node table
that pushed the history map down the Overview, and an Alerts page that was a table you had to leave to read anything.
Twelve mock screens were drawn first (one layout per page, shared parts), the "list and detail" Alerts page and the
"node table" Nodes page were chosen, and the rest was built to match.

**One look.** A deep-green top bar, panels with a title band, a map of the chassis on the left of every page about nodes
and alerts, ink-coloured buttons. Colour is still reserved for state (red unusual, amber under review, purple cluster
event, grey normal, dashed not scored). Light only: the dark theme was removed because a second palette doubled the
screens to check and nobody had asked for it. One family, Inter Tight (bundled, OFL), replaces the two display fonts;
the old font files and licences were deleted.

**Pages.**
- **Overview.** Map and cluster events on the left. On the right: the headline, what needs attention, and the last
  24 hours as two canvases that share one time axis (`watch.js`): a line for the score of every node, with the alert
  level dashed, above the node-by-time squares. The node table moved off this page.
- **Nodes (new tab, `views.nodes`, `nodes.html`).** Every node in one table: state, chassis, a small score line for
  the last day, score, CPU, memory and the latest alert, with filter buttons and search. A "Last 24 hours" line is
  drawn from the same data as the Overview chart (`_spark`), so the two cannot disagree.
- **Alerts (`alerts.html`, one template for the list page and the detail page).** Left: tabs with counts (Active,
  Recovered, Reviewed, All), find by node or date, tick boxes for bulk review, one-click Real fault / False alarm.
  Middle: the open alert (what changed, why it was flagged, the progress strip Opened, Being looked at, Recovered,
  Reviewed, where a step nobody used is struck through). Right: the map with the alert's node outlined, the review
  form and the activity log. The list page opens the first alert beside the list, so there is no empty state to click
  through. The tab that used to be called "Needs review" is called "Recovered", the same word as the alert's status.
- **Node, Event, Reports, Settings, Users, Sign in** use the same panels. The node and event pages show the map
  (the event page outlines the nodes that changed together and dims the rest).

**Smaller screens.** Below 1120 px the top bar wraps and the navigation takes its own row; below 1000 px the side
columns stack under the main column, and on the Alerts list page the list comes first. Secondary table columns
(`.wide-only`) are hidden between 1000 and 1360 px. A throwaway script measured every page at 390, 820, 1024, 1180,
1280 and 1366 px for horizontal overflow; it found and fixed the top bar at 820 px, the nodes table at 1024 px and the
report buttons at 390 px.

**What was dropped on purpose.** Slot numbers on the map tiles (the tile shows the number from the node name instead),
the dark theme, and the second display font.

## Chassis layout (22 Oct 2026)

**Why.** The Overview can arrange nodes by chassis, but the platform had no way to learn which node sits in which chassis,
so a live installation showed one long list. The first attempt reused `NodeGroup`, which is wrong: a node's group is its
peer group, so it decides which nodes it is compared with and which pooled model scores it. Putting nodes into chassis
groups would have changed the scoring and made the evaluation results (9 of 10 found, median 285 s) stale.

**What.** A separate `Chassis` model and `Node.chassis` (migration 0008), used only for display. The administrator writes
the layout as text in the Chassis box on the Settings page, one chassis per line:

    Chassis 1: compute01-compute12
    Chassis 2: compute13-compute24, compute30

`core/services/chassis.py` parses it (ranges keep the zero padding; single names are separated by commas), checks every
name against the nodes the platform knows, refuses a node placed twice, and replaces the whole layout in one transaction.
A mistake is explained in plain words and changes nothing. The box shows the saved layout back in the same form.
The map and the node list both group by chassis, numbered chassis first (2 before 10), then nodes in no chassis last.
`NodeGroup` is no longer used for display, and a test checks that saving a layout leaves every node's group unchanged.

**Later.** Chassis temperatures and fan modules belong to a chassis rather than a node (see requirements-status). They can
attach to this model.

## Time zone (22 Oct 2026)

**Why.** Every screen showed UTC, two hours behind South Africa, so the administrator had to add two hours to every
alert, event and chart before it meant anything.

**What changed.** The time zone people see is the `TIME_ZONE` setting. It defaults to `Africa/Johannesburg`
(SAST) and can be changed in `.env`; an unknown name falls back to UTC with a warning instead of stopping the platform.
Everything else follows from it:

- Templates convert on display (Django's `date` filter) and write the zone's short name (`{{ tz_label }}`, from
  `core/context.py`) instead of "UTC".
- The map and the line charts draw in that zone whatever the browser's own zone is. The page hands the zone name to the
  scripts (`data-tz` on the map, `core/templates/_chart.html` for the charts) and they use `Intl.DateTimeFormat`.
  Checked in a browser set to New York time.
- Report days run from local midnight to local midnight, so "Wednesday" is Wednesday as lived. Saved reports written for
  UTC days would overlap the new days, so `generate_due` removes any saved report that does not start at local midnight
  (`drop_misaligned`, tested) and writes it again from the stored readings. A report that already fits is never touched.
- The date fields on the Settings page are filled in, labelled and read in the same zone. The report CSV headings say
  which zone their times are in (`opened_sast`).
- New model labels (`auto-20261022-1135`) are named in local time. Existing labels keep their UTC-based names.

**What did not change.** Storage, the one-minute grid, the quarter-hour map columns and the worker schedule are UTC. So
are times in an experiment-log CSV that carry no zone (the cluster's `tools/fault_run.sh` writes UTC), and the
`inventory` command, which says UTC in its output. Both are stated in the places they appear, because reading a CSV in
local time would shift every evidence file that was already recorded.

## Experiment log: intensity text

`ExperimentEntry.intensity` was limited to 20 characters. SQLite, used in the tests, does not enforce that limit
and PostgreSQL does, so the 21 Oct log (which described each fault in a sentence) imported in the tests and failed
with a server error in production. The limit is now 200 characters, and an over-long value is refused with a message
naming the row, so the file is never half-imported and the person is told what to shorten.

## Start-up hold-back

Baselines are learned from nodes that have settled. A node in its first hour after boot is busier in ways no job
explains (NFS and network activity, daemons starting), and Ranger is switched on every day. After the morning boot on
23 Oct, 14 idle nodes were flagged together and 15 raised their own alerts about an hour after boot, almost all for
NFS request variation. The platform now leaves a node alone for `startup_minutes` after it boots: its windows are
stored as Starting up, never raise an alert, and are left out of learning. The boot time comes from
`node_boot_time_seconds` (`cluster.uptime_query` can replace the query), so the platform still needs only Prometheus.

If the boot time cannot be read, or a node has no boot time, nothing is held back, because a failed query must never
silence a node (NFR-08). The cost is that a real fault in the first minutes after boot goes unreported; the node shows
as Starting up until the period ends. The default of 60 minutes is a starting value to be tuned from measured boots.

## Normal is green

Normal nodes were a warm grey and Not scored nodes a dashed outline on the same paper, so at a glance the two read
as one. Normal is now a soft green (`--norm`) on the map tiles, the timeline squares, the keys, the Normal label and the
dot in the Nodes list. It stays light, with dark green text, so that the saturated red, amber and purple still carry
the eye, and a person who cannot tell red from green can still separate them by lightness and by the written state
beside every dot. Not scored keeps its dashed outline, and its timeline squares stay a cool grey.

## Temperatures, fans and Slurm (planned, not started)

**Status.** Deferred until after the presentations, so that the scope of the evaluated platform stays fixed. Nothing here is built.
The 21 Oct results (9 of 10 faults found, median 285 s, 84 false alerts per node-week) were measured without it and stay valid
only while these readings stay out of the combined score.

**Why it matters.** Ranger is switched on about 7 hours a day because its cooling is not stable. A cooling failure, or a node
that overheats while idle, is the fault the site cares about most, and the platform cannot see it today.

**Three things that would cause false alarms if temperatures were simply added to the metric list.**
1. After a job ends the CPU is idle but the node is still hot. Scoring it as idle straight away would alert at every job end.
2. A cooling failure is a cluster-wide event. Cluster events hold back per-node alerts, which is wrong here: a cooling event
   must raise an alert.
3. Some days the platform learns from may already contain hot periods, so it could learn "hot" as normal.

**Phases.**
1. Get the readings into Prometheus (the platform only reads Prometheus). Check first what node_exporter already reports
   (CPU temperature, thermal-throttle counters). The IPMI readings the Ranger audit script takes need a small collector or an
   IPMI exporter. Poll about once a minute. The BMC password must be changed first.
2. Collect a few days and measure: how fast a node heats after a job starts, how long it takes to cool after one ends, how much
   ambient moves through the day. These set the cool-down hold-back and the smallest change worth alerting on.
3. Shadow mode: score the readings, record them, raise no alerts, and count what would have fired per node-week the way the
   21 Oct evaluation did.
4. Cooling alerts on, for the chassis and for the node.
5. Slurm, as an optional extra source (an exporter into Prometheus). With it absent the platform behaves as now.

**Rules to keep false alarms low.** Judge a node's temperature against its chassis ambient. Alert on high and flat or rising,
not high and falling. Compare the fan modules of a chassis with each other. Take the vendor's own warning and critical limits
as a backstop. Treat a missing sensor reading as missing. Keep the 3-check sustain rule. Use the start-up hold-back for the
warm-up after boot and check whether 60 minutes is enough. The chassis ambient sensor and several idle nodes of a chassis
warming together are the main signals for an air-conditioning failure; the rate of rise matters as much as the level.

**What Slurm would add.** Exact job start and end times (a precise cool-down hold-back, job markers on charts); a cross-check
of Slurm state against CPU use (a stuck job, a stray process); nodes an administrator drained or took down are not treated as
faults; repeated job failures on one node. CPU-based load state stays primary, because the baselines were learned on it and
Slurm "allocated" does not mean the CPU is busy.

**Other readings, by value.** Highest value and lowest false-alarm risk: thermal-throttle counters, BMC event log entries,
power-supply status and power draw, fan failure, read-only filesystems, failed services (slurmd, munge). Next: pressure-stall
metrics, uncorrectable memory errors, network link state, disk SMART. Each needs its own minimum-change floor; add one group at a
time and re-measure.

## Email alerts (23 Oct 2026)

**Why.** An alert that nobody is looking at is not an alert. The platform now emails the people an administrator names, so a
problem reaches them without the screen being open. SMS and push messages are deferred; email needs no paid account and
the sender can be changed in `.env` without touching the code.

**Where it sits.** After detection, as its own worker step (`Worker.notify`, every 60 seconds, `services/notify.py`). It only
reads alerts and events that already exist, in live mode, so it cannot change a score, an alert or an evaluation result (replay
does not use it). A mail server that is down, slow or refusing the login cannot hold up a detection cycle: the step has its own
timeout (20 s), catches every error, and the worker keeps going.

**What is sent.**
- One email per alert, once (`Alert.notified_at`). Alerts that open in the same minute share one email. An alert that ends,
  comes back or is acknowledged sends nothing more.
- One email per cluster-wide event, not one per node (`ClusterEvent.notified_at`); the alerts held back during an event send
  nothing. This can be turned off with the "Also tell me when many nodes become unusual at once" box. It matters because a cooling
  failure shows up as an event, not as a few alerts.
- Nothing for an alert that opened before the switch was turned on (the platform stores when it was turned on), that is older than
  an hour, that is already being looked at, that has already ended, or that came from a replay.
- At most 6 emails an hour. What waits is sent when the hour allows, while it is still news. Failed attempts and test emails do
  not count towards the limit.
- After a failed send it waits 5 minutes before trying again, and the alert stays unsent until a send works.

**Settings.** On the Settings page (administrators only): on/off, up to 10 addresses, the event option, a "Send a test email"
button that sends to the addresses as typed (without saving them), and the last five attempts with their result. Every attempt is
recorded (`Notification`). The mail server, port, login and password are in `.env` (`EMAIL_HOST`, `EMAIL_PORT`,
`EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS` or `EMAIL_USE_SSL`, `EMAIL_FROM`) and `SITE_URL` gives the links their
address. The password is never stored in the database, shown on a page, put in a log or put in an email: an error from the mail
server has it removed before it is recorded. A node name cannot add a header to an email (control characters are removed).

**Found on Ranger, 23 Oct 2026.** The first test email failed with `SSLEOFError: UNEXPECTED_EOF_WHILE_READING`. From the head
node, `openssl s_client -starttls smtp` to smtp.gmail.com worked, but the same step from inside the container did not. The
container used OpenSSL 3.5.7 (the plain `python:3.12-slim` tag had moved to a newer Debian); the head node's own OpenSSL is older.
The likely cause is the larger handshake message of the newer OpenSSL being cut on the way out, but that was not proved. The
Dockerfile is therefore pinned to `python:3.12-slim-bookworm` (OpenSSL 3.0). If email stops working after an image change,
run the `smtplib` STARTTLS check from `podman exec` first.

**Not done.** SMS and push messages. A rule to send only when an alert has lasted some minutes. A message when an alert ends.
Choosing recipients per user rather than one list. After running for a few days on the real cluster, the number of emails per
day is the thing to look at; if it is too many, the "lasted some minutes" rule is the next step.
