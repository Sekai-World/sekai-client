# Phase 5 Production Acceptance Runbook

Read-only, repeatable acceptance for the Phase 5 shared-client topology
(per-region lifecycle, readiness/liveness, and controlled PM2/Gunicorn
deployment). This runbook accompanies the tool
[`deployment/phase5_acceptance.py`](../deployment/phase5_acceptance.py).

Scope of this runbook:

- Controlled, non-mutating validation of the deployed shared-client processes.
- Public health verification using read-only probes only.
- One-region canary and rollback gates for expansion.

This runbook does **not** claim production completion. Production acceptance
evidence must be recorded by operators only after running the tool against the
real environment; this document contains no such evidence, no real endpoints, no
credentials, and no identifiers.

## Prerequisites / preflight

- A host with read access to the PM2 process list (`pm2 jlist` is read-only and
  does not mutate services).
- The PM2 binary path, if not on `PATH` (placeholder: `<PM2_BINARY>`).
- The public health base URL of the deployed API, if public health verification
  is in scope (placeholder: `<PUBLIC_HEALTH_BASE_URL>`).
- For offline/static validation (no public endpoint reachable), use the
  `--allow-not-run` flag so the health section is permitted to report
  `not_run` without failing acceptance.
- Operator access to record aggregate acceptance results in the private
  operator record. Do **not** publish URLs, response bodies, credentials,
  process IDs, filesystem paths, exact timestamps, or detailed counters.

## Command examples (placeholders only)

Run PM2/Gunicorn validation with health left `not_run` (offline/static):

```bash
python deployment/phase5_acceptance.py \
  --pm2-binary <PM2_BINARY> \
  --allow-not-run \
  --format json
```

Run full acceptance including public health verification:

```bash
python deployment/phase5_acceptance.py \
  --pm2-binary <PM2_BINARY> \
  --health-base-url <PUBLIC_HEALTH_BASE_URL> \
  --health-timeout 5.0 \
  --format json
```

The same checks can be driven by environment variable instead of the flag:

```bash
export PHASE5_PUBLIC_HEALTH_BASE_URL=<PUBLIC_HEALTH_BASE_URL>
python deployment/phase5_acceptance.py --pm2-binary <PM2_BINARY> --format text
```

Exit code is non-zero when any check fails. A `not_run` health result fails
acceptance unless `--allow-not-run` is supplied.

## Controlled PM2 / Gunicorn checks

The tool invokes `pm2 jlist` (read-only) and validates, for each formal
shared-client process (`sharedApiClient-jp`, `sharedApiClient-en`,
`sharedApiClient-tw`, `sharedApiClient-kr`):

- **Presence**: all four expected region processes are present.
- **Online status**: each is reported `online` by PM2.
- **Gunicorn worker count**: the launch script uses exactly `--workers 1`.
- **Loopback bind**: the launch script binds to `127.0.0.1` (loopback only).
- **Config flag**: the launch script uses `--config gunicorn_conf.py`.

No PM2 start/reload/stop/delete command is issued. The check is safe to run
against production at any time.

## Public health verification

When a public health base URL is provided, the tool performs read-only
`GET` requests, using only the standard library HTTP client, against:

- `<PUBLIC_HEALTH_BASE_URL>/health/live`
- `<PUBLIC_HEALTH_BASE_URL>/health/ready`

Behavior:

- Only `GET` is used; no mutating endpoint is ever called.
- A bounded per-request timeout is applied (default 5.0s).
- Only `http`/`https` URLs are accepted; non-HTTP schemes (e.g. `file://`) are
  rejected as a failure, never opened.
- The result is derived from HTTP status and an aggregate health state only.
  Response bodies are never parsed into the report and never printed.
- Malformed HTTP responses, network errors, or timeouts are reported as a
  failure, not as `not_run`.
- When no URL is configured, the health section is `not_run`; this fails
  acceptance unless `--allow-not-run` is set (offline/static validation).

`/health/live` confirms process liveness without regional RPC. `/health/ready`
confirms aggregate, per-region readiness (fail-safe: non-200 when any region is
not ready). Both must return success for the health section to pass.

## Monitoring integration

- Emit the tool's aggregate JSON to the monitoring pipeline as an acceptance
  signal rather than raw process details.
- Track the three section states (`pm2`, `gunicorn`, `health`) and the overall
  `status` as gauges/alerts.
- Alert on any section transitioning to `fail`. A `not_run` health section should
  alert only when full acceptance (no `--allow-not-run`) is expected.
- Keep the operator record of actual run output private; monitoring should expose
  only aggregate status and small aggregate counts, never URLs, bodies, or IDs.

## One-region canary and rollback gates

For expanding the shared-client topology to an additional region (one region at
a time):

1. **Pre-activation gate** — before activation, confirm the new region's
   inventory, lease-scoped token (if using the remote account provider),
   region-specific configuration, and rollback artifacts are prepared.
2. **Activate** the new region's shared-client process using the existing PM2
   template layout; do not change other regions in the same change.
3. **Observe** the new region through at least one scheduled update cycle and
   the canary observation window:
   - Run this acceptance tool; expect all four shared-client processes present
     and online, the new region's worker/bind/config valid, and public health
     `ready`.
   - Confirm no unexplained consumer restarts and that lease/inventory health
     stay within acceptance criteria (aggregate only).
4. **Rollback gate** — retain rollback artifacts through the later of the
   observation window or one scheduled update cycle. If the new region fails
   readiness, shows unexplained restarts, or breaches lease/inventory criteria,
   deactivate the region and restore the prior known-good PM2 configuration;
   record the aggregate outcome privately.

Public endpoint verification and deployment monitoring acceptance remain part
of Phase 5 and are completed only when operators run this tool and record the
aggregate evidence.

## Recording acceptance evidence

After running the tool in production:

- Store the tool's JSON output (aggregate only) in the private operator record.
- In the public roadmap, record only that the run was performed and its aggregate
  outcome; never paste URLs, bodies, credentials, process IDs, paths, exact
  timestamps, or detailed counters.
- Mark Phase 5 production acceptance complete only when both the controlled
  PM2/Gunicorn checks and the public health verification pass in the real
  environment, and the one-region canary/rollback gates are satisfied.
