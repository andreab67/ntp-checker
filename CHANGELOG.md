# Changelog

All notable changes to **NTP Checker** are documented in this file.

The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `LICENSE` (BSD 3-Clause).
- `CONTRIBUTORS.md` listing the project maintainer and how to be added.
- `CHANGELOG.md` (this file).
- Full rewrite of `README.md` documenting components, environment
  variables, quick start, container build, Kubernetes deployment, and CI.
- GitHub Actions workflow `.github/workflows/ci.yml` with `validate`,
  `build` (mock), and `security` jobs.

### Removed
- `.gitlab-ci.yml` — superseded by the GitHub Actions workflow.

## [0.1.0] - 2026-04-18

### Added
- `monitor.py` — SSH-based NTP/GPS health checker that runs
  `chronyc tracking`, `chronyc sources -n`, and `gpspipe` on a remote
  host, validates leap status, stratum, offset, source count, and GPS
  fix, and sends email alerts via Amazon SES.
- `app.py` — Flask dashboard exposing `GET /`, `GET /api/latest`, and
  `GET /api/offset?window=24h|14d|90d&interval=5min|1h|1d` with Plotly
  charts.
- `postgres/schema.sql` — `metrics` schema with range-partitioned
  `ntp_parent` table plus `metrics.create_daily_partition(date)` and
  `metrics.maintain_partitions(retention_days, premake_days)` helpers,
  scheduled via `pg_cron`.
- `Dockerfile.checker` and `Dockerfile.dashboard` — two Python 3.11
  slim images running as non-root.
- Kubernetes manifests: `kubernetes/deployment.yaml`,
  `kubernetes/service.yaml`, `kubernetes/ingress.yaml`,
  `kubernetes/cronjob.yaml`.
- `.gitlab-ci.yml` — initial GitLab CI with Kaniko builds, security
  template includes, and `kubectl apply` deploy on `main`.

[Unreleased]: https://github.com/andreaborghi/ntp-checker/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/andreaborghi/ntp-checker/releases/tag/v0.1.0
