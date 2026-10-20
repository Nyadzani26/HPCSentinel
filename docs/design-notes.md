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
