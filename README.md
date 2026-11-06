# HPCSentinel

A cluster-agnostic anomaly detection platform for HPC administrators. It reads the telemetry
a facility already keeps in Prometheus, learns what is normal for each node without needing
any record of failures, flags unusual behaviour with an explanation, and records the
administrator's review. It installs nothing on the monitored nodes and sends them no commands.

This repository is the software submitted for NPRJ740 (Sol Plaatje University, Dr Silas Verkijika).
The design it follows is in the final report (Sections 3 to 5); `docs/requirements-status.md`
records, requirement by requirement, what is implemented and what evidence exists. The submitted
version is the tag `v1.0.0`.

## Architecture in one paragraph

Three long-running containers built from one image: **db** (PostgreSQL), **web** (Django served
by Gunicorn) and **worker** (`manage.py run_worker`, the scheduler that drives the detection
cycle). The web application and the worker never call each other; they cooperate only through
the database (design decision D-3). A short-lived **migrate** container prepares the database.
The only external system is Prometheus, queried read-only.

## Quick start

Requirements: Docker or Podman with Compose, and network access from the host to Prometheus.
(The reference deployment on Ranger runs under rootless Podman with `podman-compose`; replace
`docker compose` with `podman-compose` below.)

```bash
cp .env.example .env                                    # then edit: SECRET_KEY, POSTGRES_PASSWORD, PROMETHEUS_URL
cp config/hpcsentinel.example.yml config/hpcsentinel.yml  # then edit: cluster.selector, node name mapping, metrics
docker compose up -d --build
docker compose run --rm web python manage.py createsuperuser   # creates an Administrator
```

Open `http://<host>:8000/` and sign in. Check the worker with `docker compose logs -f worker`.
The `createsuperuser` account is for the Django administration site (`/admin/`); to give people
access to the platform itself, sign in with it and add users on the Users page.

Notes:

* Email alerts are optional. Fill in the `EMAIL_` lines in `.env` (see `.env.example`; for Gmail use an app password, never the
  account's own password) and set the addresses on the Settings page. With `EMAIL_HOST` empty nothing is sent.
* `TIME_ZONE` is the time zone shown on screen and used for report days. It is `Africa/Johannesburg` (SAST) unless you set
  another name from the IANA list in `.env`; everything stored is UTC either way.
* `PROMETHEUS_URL` is the address of Prometheus **as seen from inside the containers**. For a
  Prometheus running on the same host, `http://host.docker.internal:9090` works (the Compose file
  maps that name to the host). If the host firewall blocks the Docker bridge, allow it to reach
  port 9090.
* The release serves plain HTTP. Place it behind the facility's TLS-terminating proxy for any use
  beyond a trusted management network (see report Section 5.7 and `docs/deployment-behind-apache.md`).

## Operating notes

* **Where the data lives.** Everything the platform remembers is in the `platform_data` volume (PostgreSQL) and the
  `model_store` volume (learned models). Prometheus keeps the raw measurements in its own storage. There is **no
  backup routine**. A copy of the database can be taken with
  `docker compose exec db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > backup.sql`.
* **Rootless Podman.** On 3 November 2026 the reference deployment lost all of its stored data because Podman's
  container storage was under `/tmp`, which the host cleans every day. Keep Podman's storage in the user's home
  folder (`graphroot` in `~/.config/containers/storage.conf`), and run `loginctl enable-linger <user>` so the
  containers keep running when the user logs out.
* **After a loss or a new database** the platform learns again by itself from what Prometheus still holds; its
  progress is shown on the Settings page. How far back it can learn is limited by Prometheus's retention.
* **Known gaps.** No sign-in lockout, no second factor, sign-ins are not logged (report Section 5.7).

## What you can do in the web interface

* **Overview** (home): the chassis map, what needs attention, and the last 24 hours (the score of every node, and what each node was doing), with a banner when Prometheus is unreachable.
* **Nodes**: every node in one table, with its state, a small line for the last day, score, CPU, memory and latest alert, and filters and search.
* **Node page**: load state, the three scores, a score chart with the alert threshold, and one chart per metric with a red line where an alert was raised.
* **Alerts**: a list on the left (Active, Recovered, Reviewed, All; filter by node and date) and the open alert beside it. Each alert shows why it was raised (baseline, observed value, deviation, load state, top contributing metrics, the node's history) and is acknowledged, closed with an outcome (Confirmed or False alarm), noted, or reopened by an Administrator. Every action records who and when.
* **Cluster events** (listed on the Overview and the Alerts page, each with a detail page): when many nodes become unusual together, one event is recorded instead of one alert per node.
* **Reports**: a daily, weekly and monthly report written by the platform, each kept as it was written, with a printable view and CSV downloads.
* **Settings** (Administrator, address `/controls/`): Prometheus address and test, alert sensitivity (threshold and persistence), the start-up hold-back, email alerts (on or off, recipients, which messages), the chassis layout, the data-readiness report, and under Advanced the train / replay / evaluate requests, excluded periods and the experiment-log upload. Learning itself is automatic.
* **Users** (Administrator): create users, set roles, deactivate.

## Trying it without a cluster

```bash
docker compose run --rm web python manage.py seed_demo --hours 6
```

builds a synthetic fleet, trains on three normal days and runs the real live cycle with two injected faults, so every screen has data. It never contacts Prometheus.

## Configuration

`config/hpcsentinel.example.yml` is generic. `config/hpcsentinel.ranger.yml` is the same file adapted to the SPU Ranger test cluster (job selector and node naming only).

`config/hpcsentinel.yml` defines which metrics are used, how Prometheus names the nodes and how
the detector behaves (retrieval interval, persistence windows, threshold). Every query must contain
`$SELECTOR`, which limits it to the configured nodes. A malformed file is rejected with a message
naming the problem. Changes are stored as versioned configurations and every alert records the
version that produced it.

## Tests

```bash
pip install -r requirements.txt
SECRET_KEY=test python manage.py test
```

The tests use SQLite and need no running services. On 6 November 2026 there were 316 tests and
all passed (about five minutes). They cover the database constraints that carry business rules, the
alert lifecycle and its permissions, configuration validation, the read-only Prometheus client, the
outage handling of the detection cycle, learning, reports, email, cluster events and the screens.
The tests run on SQLite while the platform runs on PostgreSQL, so a difference between the two is
not caught by them (report Section 7.4, D-1).

## Layout

| Path | Contents |
|---|---|
| `core/models.py` | Data model (report Figure 10) with constraints for BR-05 to BR-08 |
| `core/services/review.py` | `ReviewService`: the only way alerts change (workflow 2) |
| `core/services/prometheus.py` | Read-only Prometheus client |
| `core/pipeline/cycle.py` | `DetectionCycle` (workflow 1) |
| `core/config.py` | Configuration loading and validation |
| `core/management/commands/run_worker.py` | The worker process |
| `config/hpcsentinel.example.yml` | Example configuration |
| `core/pipeline/` | Features, detector, alert rules, learning data cleaning, replay and evaluation |
| `core/services/` | Automatic learning, cluster events, reports, email, settings |
| `config/hpcsentinel.ranger.yml` | The configuration used on the SPU Ranger cluster |
| `tools/` | Scripts used to run the fault campaign on Ranger |
| `docs/` | Requirements status, design notes, deployment behind Apache, baseline collection |
