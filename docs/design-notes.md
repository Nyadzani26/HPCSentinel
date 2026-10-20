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
