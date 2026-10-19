# HPCSentinel

A cluster-agnostic anomaly detection platform for HPC administrators. It reads the telemetry
a facility already keeps in Prometheus, learns what is normal for each node without needing
any record of failures, flags unusual behaviour with an explanation, and records the
administrator's review. It installs nothing on the monitored nodes and sends them no commands.

This repository is the software submitted for NPRJ740. The design it follows is in the final
report (Sections 3 to 5); `docs/requirements-status.md` records, requirement by requirement,
what is implemented.

## Architecture in one paragraph

Three long-running containers built from one image: **db** (PostgreSQL), **web** (Django served
by Gunicorn) and **worker** (`manage.py run_worker`, the scheduler that drives the detection
cycle). The web application and the worker never call each other; they cooperate only through
the database (design decision D-3). A short-lived **migrate** container prepares the database.
The only external system is Prometheus, queried read-only.

## Quick start

Requirements: Docker with the Compose plugin, and network access from the host to Prometheus.

```bash
cp .env.example .env                                    # then edit: SECRET_KEY, POSTGRES_PASSWORD, PROMETHEUS_URL
cp config/hpcsentinel.example.yml config/hpcsentinel.yml  # then edit: cluster.selector, node name mapping, metrics
docker compose up -d --build
docker compose run --rm web python manage.py createsuperuser   # creates an Administrator
```

Open `http://<host>:8000/` and sign in. Check the worker with `docker compose logs -f worker`.

Notes:

* `PROMETHEUS_URL` is the address of Prometheus **as seen from inside the containers**. For a
  Prometheus running on the same host, `http://host.docker.internal:9090` works (the Compose file
  maps that name to the host). If the host firewall blocks the Docker bridge, allow it to reach
  port 9090.
* The release serves plain HTTP. Place it behind the facility's TLS-terminating proxy for any use
  beyond a trusted management network (see report Section 5.7).

## Configuration

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

The tests use SQLite and need no running services. They cover the database constraints that
carry business rules, the alert lifecycle and its permissions, configuration validation, the
read-only Prometheus client and the outage handling of the detection cycle.

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
| `docs/` | Requirements status |
