# TW Remote Account Provider Canary

This runbook prepares a one-region, one-consumer canary. It does not authorize
changing production automatically. Keep JP, EN, and KR on the local provider
throughout the canary.

## Preconditions

- Both repositories are deployed from revisions containing the remote provider,
  24-hour lease support, and lease lifecycle handling.
- The account service uses Postgres and has a separately managed credential
  encryption key.
- Legacy TW JSON state has a verified backup and has been migrated successfully.
- At least one TW account is `AVAILABLE` and no unexpected active lease exists.
- The canary token has lease capability only; it cannot provision or administer
  accounts.
- The service URL is HTTPS and reachable from the shared-client host.
- The current protected PM2 configuration and process description have
  been copied to a protected rollback location.
- The lease journal directory is persistent, writable only by the service user,
  and is not placed under `/tmp`.

The public ingress intentionally routes only `/v1` to the service; `/healthz`,
`/readyz`, and `/metrics` are cluster-only. Verify them from an authorized
Kubernetes operator context without requesting a lease:

```bash
kubectl -n <account-service-namespace> exec deployment/<account-service-deployment> -- \
  python -c "import urllib.request; print(urllib.request.urlopen( \
  'http://service-local/healthz').read().decode())"
kubectl -n <account-service-namespace> exec deployment/<account-service-deployment> -- \
  python -c "import urllib.request; print(urllib.request.urlopen( \
  'http://service-local/metrics').read().decode())" \
  | grep -E '^pjsk_account_(inventory_accounts|active_leases)'
```

Do not use the acquire endpoint as a connectivity probe because it changes
inventory state.

## Render and Review

Export `AES_KEY`, `INTERNAL_RPC_TOKEN`, `SEKAI_ACCOUNT_SERVICE_URL`, and
`SEKAI_ACCOUNT_SERVICE_TOKEN`, then render the dedicated template outside the
repository:

```bash
umask 077
envsubst < deployment/pm2/canary/sharedApiClientTW.yaml.example \
  > <protected-ops-dir>/sharedApiClientTW.remote-canary.yaml
chmod 600 <protected-ops-dir>/sharedApiClientTW.remote-canary.yaml
```

Before applying it, confirm all placeholders were replaced, the bind remains
loopback-only, `--workers 1` remains present, and no
`SEKAI_TW_ACCESS_TOKEN` or `SEKAI_TW_SDK_OPEN_ID` is present. Never print the
rendered file into shared logs or CI output.
Confirm `--config gunicorn_conf.py` and `SEKAI_ACCOUNT_LEASE_STATE_DIR` are
present so graceful release and crash-boundary recovery remain enabled.

## Controlled Activation

During an announced window, replace only `sharedApiClient-tw` using the rendered
canary file. When the cwd or credential set changes, PM2 reload retains stale
process metadata and environment values; use `pm2 delete sharedApiClient-tw`
followed by `pm2 start <canary-file> --only sharedApiClient-tw`. Do not restart
check-update, event-tracker, or other regions. Record the activation timestamp,
previous PM2 restart count, deployed revisions, and account-service inventory
counts.

Immediately verify:

- the process is online with one Gunicorn worker and the same loopback port;
- shared-client liveness succeeds;
- `ensure_ready` completes and readiness becomes `READY`;
- exactly one successful TW acquire is recorded;
- one TW active lease exists and no lease conflict or quarantine occurred;
- logs contain no bearer token, game credential, account ID, or lease ID.

Continue observation through at least one scheduled update cycle. Track process
restarts, readiness, acquire latency/failures, active lease count, quarantine
events, queue rejection/timeouts, check-update success, and event-tracker
delivery. Do not expand the canary while any unexplained regression remains.

## Public Observation Record (2026-08-17)

The TW single-consumer canary exceeded the 24-hour observation gate. Public
documentation records only the decision-level result; exact timestamps,
revision identifiers, image digests, host paths, lease identifiers, and detailed
counter values remain in the private operator record.

- Client and account-service builds were the approved production revisions.
- The shared client stayed online with one worker and no process restarts.
- The account-service pod stayed ready with no container restarts.
- TW inventory and lease health remained within the canary gate. No account
  service error, failure, or quarantine signal was observed in the operator
  metrics snapshot.
- The event-ranking SQLite outbox was not deployed in this account-provider
  canary, so this record does not claim event-tracker outbox delivery.

The local-provider rollback artifact, rendered remote configuration, lease
journal backup, and legacy inventory backup remain protected outside the
repository throughout the rollback window.

## Rollout Gate for the Next Region

Before activating a next-region canary, record all of the following
pre-activation gates:

1. At least one `AVAILABLE` account and a lease-scoped token for that region.
2. A region-specific PM2 render with loopback binding, one worker, and
   persistent lease state.
3. Protected local-provider configuration, lease journal, and inventory
   rollback artifacts.
4. A pre-activation snapshot of service version, pod readiness/restarts,
   account inventory, and current local-provider process state.

KR was the selected next region. All four pre-activation gates above were
recorded for KR, the one-region canary was activated, and KR completed its
observation window on 2026-08-25 without unexplained restart, authentication,
lease, or downstream update regression. The next region, if any, follows the
same sequence: record all four gates, activate as a one-region canary, and
complete the observation window before further expansion is authorized.

The rollback window is the later of 24 hours after the next-region activation
or one complete scheduled update cycle. Do not delete the local-provider
configuration, lease journal backup, or inventory backup before that point and
before an explicit acceptance record is added.

## Rollback

Rollback triggers include failure to become ready, repeated account-service
errors, unexpected lease churn, authentication failure, quarantine, new process
restarts, or downstream update/event failures.

Restore the protected original local-provider PM2 file by deleting and starting
only `sharedApiClient-tw`. Confirm readiness using the local credential path. The
remote lease should be released during graceful worker exit; if it remains,
wait for its server-side expiry or release it through an authorized operator
workflow. Do not edit Postgres lease rows manually.

Retain the account service, migrated inventory, legacy JSON backup, and local
provider configuration during the rollback window. Record the result in the
architecture and remediation roadmaps before expanding to another consumer or
region.
