# NTP Checker

[![License: BSD 3-Clause](https://img.shields.io/badge/License-BSD_3--Clause-blue.svg)](LICENSE)
[![CI](https://github.com/andreaborghi/ntp-checker/actions/workflows/ci.yml/badge.svg)](.github/workflows/ci.yml)

A Kubernetes-native system that continuously verifies the health of a remote
**NTP / GPS** time source, stores every sample in **PostgreSQL**, and
visualizes the history through a **Flask + Plotly** dashboard. Designed to
run as two containers (checker + dashboard) side-by-side in a single cluster.

---

## Table of contents

1. [Features](#features)
2. [Architecture](#architecture)
3. [Repository layout](#repository-layout)
4. [Components](#components)
5. [Configuration (environment variables)](#configuration-environment-variables)
6. [Quick start (local)](#quick-start-local)
7. [Container build](#container-build)
8. [Kubernetes deployment](#kubernetes-deployment)
9. [Dashboard](#dashboard)
10. [CI/CD (GitHub Actions)](#cicd-github-actions)
11. [Contributing](#contributing)
12. [Changelog](#changelog)
13. [License](#license)

---

## Features

- SSH-based polling of a remote NTP/GPS host — no agent required on
  the target.
- Parses `chronyc tracking` and `chronyc sources -n` plus GPS TPV data
  from `gpspipe` (JSON mode).
- Validates **leap status**, **stratum bounds**, **absolute time offset**,
  **number of sources**, **selected source**, and **GPS fix**.
- Email alerts via **Amazon SES** (SMTP interface) when any of the
  above fails.
- PostgreSQL storage with **daily range partitions** and automatic
  partition creation / retention via `pg_cron`.
- Flask JSON API + Plotly dashboard with **24 h**, **14 d**, and
  **90 d** windowed offset views (avg / p95 / max).
- Two-container image layout, non-root users, ready for Kubernetes.
- GitHub Actions CI with explicit **validation**, **build** (mock), and
  **security** stages.

---

## Architecture

![Architecture](architecture.jpg)

---

## Repository layout

| Path | Purpose |
| --- | --- |
| [monitor.py](monitor.py) | SSH-based NTP/GPS health checker with SES alerting and PostgreSQL writes. |
| [app.py](app.py) | Flask dashboard and JSON APIs. |
| [Dockerfile.checker](Dockerfile.checker) | Container image for `monitor.py`. |
| [Dockerfile.dashboard](Dockerfile.dashboard) | Container image for `app.py`. |
| [postgres/schema.sql](postgres/schema.sql) | PostgreSQL schema, partition helpers, and pg_cron job. |
| [kubernetes/deployment.yaml](kubernetes/deployment.yaml) | Checker + dashboard deployment. |
| [kubernetes/service.yaml](kubernetes/service.yaml) | ClusterIP service fronting the dashboard. |
| [kubernetes/ingress.yaml](kubernetes/ingress.yaml) | NGINX ingress for external access. |
| [kubernetes/cronjob.yaml](kubernetes/cronjob.yaml) | Scheduled housekeeping job. |
| [.github/workflows/ci.yml](.github/workflows/ci.yml) | GitHub Actions pipeline (validate / build-mock / security). |
| [architecture.jpg](architecture.jpg) | Architecture diagram. |
| [dashboard.jpg](dashboard.jpg) | Dashboard screenshot. |
| [LICENSE](LICENSE) | BSD 3-Clause license. |
| [CONTRIBUTORS.md](CONTRIBUTORS.md) | Project contributors. |
| [CHANGELOG.md](CHANGELOG.md) | Release history. |

---

## Components

### `monitor.py` — the checker

Long-running loop that every `CHECK_INTERVAL_SEC` seconds:

1. Runs `chronyc tracking` on the remote host via SSH and parses
   **Leap status**, **Stratum**, and **Last offset**.
2. Runs `chronyc sources -n` and counts sources / detects a selected
   source.
3. Runs `gpspipe -w -n $GPSPIPE_SAMPLES` (wrapped in `timeout
   $CGPS_TIMEOUT_SEC`) and parses TPV JSON to determine the GPS fix mode.
4. Applies validation rules:
   - Leap status must contain `Normal`.
   - Stratum must be `<= MAX_STRATUM`.
   - `abs(last_offset) <= MAX_ABS_OFFSET_SEC`.
   - At least one selected source and `total_sources > 0`.
   - GPS mode `>= 2` (2D or 3D fix).
5. On success: writes a row into `metrics.ntp_parent`.
6. On failure: logs the reason and sends an SES email.

### `app.py` — the dashboard

A tiny Flask application with three routes:

| Route | Method | Description |
| --- | --- | --- |
| `/` | GET | Self-contained HTML page with Plotly charts (no build step). |
| `/api/latest` | GET | Most recent row of `metrics.ntp_parent` as JSON. |
| `/api/offset?window=<w>&interval=<i>` | GET | Time-bucketed aggregates: `avg`, `p95`, and `max` of the absolute offset. |

Accepted values:

- `window`: `24h`, `14d`, `90d`
- `interval`: `5min`, `1h`, `1d`

### `postgres/schema.sql`

Creates schema **`metrics`** and a **range-partitioned** parent table
`metrics.ntp_parent` keyed by `ts` (one partition per UTC day). Ships
two helpers:

- `metrics.create_daily_partition(d date)` — idempotently creates the
  partition for day `d`.
- `metrics.maintain_partitions(retention_days int, premake_days int)` —
  pre-creates future partitions and drops partitions older than the
  retention window.

A **pg_cron** job schedules `metrics.maintain_partitions(...)` to run
daily, giving rolling retention with no manual intervention.

---

## Configuration (environment variables)

All configuration is via environment variables. Defaults are taken
directly from the source.

### `monitor.py`

| Variable | Default | Description |
| --- | --- | --- |
| `NTP_HOST` | `myntp` | Hostname of the remote NTP/GPS machine (reached via SSH). |
| `NTP_IP` | `0.0.0.0` | Display-only IP used in alert messages. |
| `SSH_USER` | `ubuntu` | SSH login user. |
| `SSH_PORT` | `22` | SSH port. |
| `CHECK_INTERVAL_SEC` | `30` | Seconds between checks. |
| `MAX_STRATUM` | `4` | Max acceptable chrony stratum. |
| `MAX_ABS_OFFSET_SEC` | `0.050` | Max acceptable `abs(last_offset)` in seconds. |
| `CGPS_TIMEOUT_SEC` | `8` | Timeout wrapping `gpspipe` on the remote host. |
| `GPSPIPE_SAMPLES` | `5` | Number of TPV samples to collect per check. |
| `LOG_PATH` | `/var/log/ntp-checker.log` | Log file path (inside the container). |
| `LOG_LEVEL` | `DEBUG` | Python logging level. |
| `PYTHONUNBUFFERED` | `1` | Forces unbuffered stdout in the container. |
| `DATABASE_URL` | _(unset)_ | `postgresql://user:pass@host:5432/ntp-checker`. If unset, DB writes are skipped. |
| `POSTGRES_TABLE` | `metrics.ntp_parent` | Target table for inserts. |
| `EMAIL_SENDER` | _(unset)_ | `From:` address for alerts (SES-verified). |
| `EMAIL_RECEIVER1` | _(unset)_ | Primary alert recipient. |
| `EMAIL_RECEIVER2` | _(unset)_ | Optional second recipient. |
| `SMTP_USERNAME` | _(unset)_ | SES SMTP username. |
| `SMTP_PASSWORD` | _(unset)_ | SES SMTP password. |

The SMTP endpoint is hard-coded to
`email-smtp.us-east-1.amazonaws.com:465` (SMTPS).

### `app.py`

| Variable | Default | Description |
| --- | --- | --- |
| `DATABASE_URL` | _(unset, required)_ | PostgreSQL DSN used for all queries. |
| `DASH_PORT` | `8080` | Port the Flask dev server binds to. |

In production the dashboard image is served by **gunicorn** on port
`8080` (see `Dockerfile.dashboard`).

---

## Quick start (local)

**Prerequisites**: Python 3.11, a reachable PostgreSQL instance,
an SSH key accepted by the remote NTP host.

```bash
# 1. Install deps
python -m pip install flask gunicorn psycopg2-binary

# 2. Create the schema
export DATABASE_URL=postgresql://user:pass@localhost:5432/ntp-checker
psql "$DATABASE_URL" -f postgres/schema.sql

# 3. Run the checker in one terminal
export NTP_HOST=my-ntp.example.com
export SSH_USER=ubuntu
python monitor.py

# 4. Run the dashboard in another terminal
python app.py
# open http://localhost:8080
```

To exercise alerting locally, also export `EMAIL_SENDER`,
`EMAIL_RECEIVER1`, `SMTP_USERNAME`, and `SMTP_PASSWORD`.

---

## Container build

Two independent images (both Python 3.11-slim, non-root user):

```bash
docker build -f Dockerfile.checker   -t ntp-checker/checker:dev   .
docker build -f Dockerfile.dashboard -t ntp-checker/dashboard:dev .
```

> **Note** — the GitHub Actions `build` job is a **mock**: it logs the
> image tag that *would* be produced but does not actually invoke
> Docker. See [CI/CD](#cicd-github-actions) for how to enable a real
> build.

---

## Kubernetes deployment

The manifests in [kubernetes/](kubernetes/) target a standard cluster
with an NGINX ingress controller.

### Required secrets

Before applying, create the secrets referenced by the deployment:

- **SSH key** + **known_hosts** — mounted into the checker container
  so it can reach the remote NTP host non-interactively.
- **Database URL** — the `DATABASE_URL` value.
- **SES SMTP credentials** — `SMTP_USERNAME`, `SMTP_PASSWORD`,
  `EMAIL_SENDER`, and recipients.

### Apply order

```bash
kubectl apply -f kubernetes/service.yaml
kubectl apply -f kubernetes/ingress.yaml
kubectl apply -f kubernetes/deployment.yaml
kubectl apply -f kubernetes/cronjob.yaml   # scheduled housekeeping
```

Both the checker and dashboard run inside the same deployment so they
share config and can be scaled together.

---

## Dashboard

![Dashboard screenshot](dashboard.jpg)

The dashboard refreshes every 30 seconds and shows:

- Current **stratum**, **leap status**, and **GPS mode** as pills.
- Offset over the **last 24 h** in 5-minute buckets.
- Offset over the **last 14 d** in hourly buckets.
- Offset over the **last 90 d** in daily buckets.

Each chart plots three series: **avg**, **p95 abs**, and **max abs**
offset.

---

## CI/CD (GitHub Actions)

The workflow at [.github/workflows/ci.yml](.github/workflows/ci.yml)
runs on every push to `main` and every pull request. It has three
jobs wired via `needs:`:

### 1. `validate` — lint & syntax

- **Ruff** on all Python files.
- `python -m py_compile monitor.py app.py` as a final syntax gate.
- **yamllint** on the `kubernetes/` manifests.
- **Hadolint** on both Dockerfiles.

These are non-blocking (`continue-on-error: true`) today so findings
surface without stopping the pipeline; remove the flag once the
codebase is clean.

### 2. `build` — MOCK container build

The `build` job runs with a matrix (`checker`, `dashboard`) and **does
not** actually build or push anything. Each job:

- Logs the image tag that *would* be produced
  (`ghcr.io/<owner>/<repo>/<target>:<sha>`).
- Writes a small artifact so the downstream `security` job can depend
  on it.

**To switch to a real build**, open
[.github/workflows/ci.yml](.github/workflows/ci.yml) and replace the
`Mock build` step with the commented-out
`docker/setup-buildx-action` + `docker/build-push-action` block at the
bottom of the file. No other changes are required — the job already
has `packages: write` permission for GHCR.

### 3. `security` — scans

- **Trivy** filesystem scan (`CRITICAL,HIGH`, SARIF output uploaded to
  the GitHub *Security* tab).
- **CodeQL** static analysis for Python.
- **Gitleaks** secret scanning.

All scan results appear under the repository's **Security → Code
scanning alerts** view.

---

## Contributing

Contributions are welcome. See [CONTRIBUTORS.md](CONTRIBUTORS.md) for
the maintainer list and instructions on how to be added. All
contributions are accepted under the BSD 3-Clause license.

Typical flow:

1. Open an issue describing the change.
2. Fork → branch → commit → open a PR against `main`.
3. Ensure the GitHub Actions pipeline is green.
4. Add your name to `CONTRIBUTORS.md` in the same PR.

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for release history. This project
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/).

---

## License

Distributed under the **BSD 3-Clause License**. See [LICENSE](LICENSE)
for the full text.
