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
- The current `<protected-ops-dir>/sharedApiClientTW.yaml` and PM2 process description have
  been copied to a protected rollback location.

Verify service health and inspect aggregate inventory metrics without requesting
a lease:

```bash
curl --fail --silent --show-error "$SEKAI_ACCOUNT_SERVICE_URL/healthz"
curl --fail --silent --show-error "$SEKAI_ACCOUNT_SERVICE_URL/metrics" \
  | grep -E 'pjsk_account_(inventory_accounts|active_leases)'
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
`loopback-service-endpoint`, `--workers 1` remains present, and no
`SEKAI_TW_ACCESS_TOKEN` or `SEKAI_TW_SDK_OPEN_ID` is present. Never print the
rendered file into shared logs or CI output.

## Controlled Activation

During an announced window, replace only `sharedApiClient-tw` using the rendered
canary file. Do not restart check-update, event-tracker, or other regions. Record
the activation timestamp, previous PM2 restart count, deployed revisions, and
the account-service inventory counts.

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

## Rollback

Rollback triggers include failure to become ready, repeated account-service
errors, unexpected lease churn, authentication failure, quarantine, new process
restarts, or downstream update/event failures.

Restore the protected original local-provider PM2 file and reload only
`sharedApiClient-tw`. Confirm readiness using the local credential path. The
remote lease should be released during graceful worker exit; if it remains,
wait for its server-side expiry or release it through an authorized operator
workflow. Do not edit Postgres lease rows manually.

Retain the account service, migrated inventory, legacy JSON backup, and local
provider configuration during the rollback window. Record the result in the
architecture and remediation roadmaps before expanding to another consumer or
region.
