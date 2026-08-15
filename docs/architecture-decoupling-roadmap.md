# Architecture Decoupling Roadmap

## Goal

Separate the game protocol client, account lifecycle, scheduled jobs, public API,
and operations UI into clear components. Move game account registration and
allocation into a separately deployed, network-accessible service in another
repository.

## Current Problems

- `APIClient` combines transport, encryption, retries, authentication, tutorial
  completion, session state, and game APIs.
- `shared_client` combines account storage and registration with lifecycle state,
  scheduling, queueing, and JSON-RPC transport.
- `check_update` combines fetching, transformation, publication, Git operations,
  recovery, and scheduling.
- Consumers depend on concrete JSON-RPC method names instead of narrow service
  interfaces.
- Protocol, deployment, Git, Strapi, and regional settings share global modules
  and import-time state.
- The public API and PM2 dashboard share one application and security boundary.

## Target Boundaries

The `sekai-client` repository should contain:

```text
src/sekai_client/
├── protocol/       # HTTP transport, encryption, regional protocol configuration
├── auth/           # Game authentication and session management
├── accounts/       # Account models and AccountProvider abstraction
├── game/           # Profiles, rankings, master data, information
└── runtime/        # Client lifecycle and serialization

apps/
├── shared_client/
├── update_worker/
├── event_tracker/
├── public_api/
└── dashboard/
```

A separate account-service repository should own:

```text
src/account_service/
├── domain/         # Account, lease, and access-token models
├── application/    # Register, acquire, release, and quarantine use cases
├── infrastructure/ # Database, encryption, and game registration adapter
└── api/            # Authenticated HTTP API and schemas
```

Dependency direction must remain:

```text
apps -> game services -> auth/session -> protocol
                     -> AccountProvider
```

## Account Service Contract

Use leases instead of returning an uncoordinated account. The minimum API is:

- `POST /v1/leases`: acquire an account by region and consumer.
- `DELETE /v1/leases/{lease_id}`: release an account.
- `POST /v1/leases/{lease_id}/invalid`: report invalid credentials or
  account failures.
- Internal administrative operations: register, inspect, quarantine, restore,
  rotate, and revoke accounts.

The acquire request should include an `Idempotency-Key`, region, consumer
identity, and requested TTL. The response should include the lease ID, expiry,
and a region-specific credential payload.

Account states:

```text
REGISTERING -> AVAILABLE -> LEASED -> AVAILABLE
                    |          |
                    +----------+-> QUARANTINED
REGISTERING -> FAILED
```

JP/EN credentials and TW/KR credentials must use distinct typed models instead
of an unrestricted dictionary.

## Security Requirements

- Require HTTPS for all non-local traffic.
- Store hashes of service access tokens and support scopes, rotation, and
  revocation.
- Encrypt game credentials at rest with a separately managed encryption key.
- Never place tokens in URLs or log credentials and response bodies.
- Return `Cache-Control: no-store` for credential responses.
- Separate account registration/admin scopes from account lease scopes.
- Rate-limit acquisition and registration independently.
- Record credential-safe audit events for acquisition, release, quarantine, and
  token administration.

## Phases

### Phase 0: Contract and Safety Baseline

- [x] Define typed `AccountCredential`, `AccountLease`, and account-service error
  models.
- [x] Define the `AccountProvider` protocol: acquire, release, and report invalid.
- [x] Specify lease expiry, reacquisition, idempotency, and failure semantics.
- [x] Add contract tests independent of HTTP and storage implementations.
- [x] Decide the account-service repository name, database, deployment target,
  and encryption-key management.

Acceptance criteria:

- The contract represents JP/EN and TW/KR credentials without optional-field
  ambiguity.
- Client code does not depend on account-service database or framework models.
- Logs and exceptions cannot expose credentials.

### Phase 1: Isolate Existing Account Sources

- [x] Move YAML and environment-variable account loading into
  `LocalAccountProvider` without changing production behavior.
- [x] Remove YAML, JWT decoding, and environment credential handling from
  `shared_client`.
- [x] Inject `AccountProvider` into the client runtime.
- [x] Add tests for acquisition failure, invalid credentials, release, and login
  rollback.

Acceptance criteria:

- `shared_client` does not know account file names or credential environment
  variable names.
- Existing deployments can continue using the local provider.

### Phase 2: Extract Protocol and Registration Logic

- [x] Split `APIClient` into protocol transport, authentication/session, and game
  API services.
- [x] Make request retry policy explicit and idempotency-aware.
- [x] Extract a minimal registration adapter without tutorial completion or
  post-login refresh side effects.
- [x] Add protocol contract tests for registration and credential validation.

Acceptance criteria:

- Account registration does not require the shared-client lifecycle or RPC app.
- A credential validity check does not run the full game login workflow.
- Protocol code has no Flask, scheduler, Git, Strapi, or dashboard dependency.

### Phase 3: Build the Separate Account Service

- [x] Create the account-service repository.
- [x] Add the account-service CI baseline.
- [x] Implement encrypted persistence, service-token authentication, scopes, and
  audit events.
- [x] Implement transactional account acquisition with exclusive leases.
- [x] Implement expiry recovery, release, quarantine, and invalid-account reports.
- [ ] Run account registration asynchronously with bounded retries and rate
  limits.
- [x] Add migrations, backup/restore instructions, health checks, metrics, and
  deployment documentation.

Acceptance criteria:

- Concurrent requests cannot lease one account twice.
- Expired leases become recoverable without manual database edits.
- Registration failure cannot publish a partial account.
- Restart, migration, token rotation, and backup recovery are tested.

Current implementation audit (2026-08-14):

- `pjsk-account-service` implements four-region typed credentials, encrypted
  SQLite/Postgres inventory, transactional leases, expiry recovery, quarantine,
  scoped authentication, safe audit events, and bounded metrics.
- JP/EN registration and TW/KR GSDK provisioning publish only complete auth
  inputs. The service does not perform game authentication.
- A backup-first migration retains and validates legacy TW/KR JSON state before
  publishing it to inventory. `/v1/token` remains a temporary internal adapter.
- This repository now implements the matching remote HTTP provider and keeps
  `/user/auth`, session establishment, tutorial work, and game APIs client-owned.

### Phase 4: Integrate the Remote Provider

- [x] Add `RemoteAccountProvider` with HTTPS, bearer-token authentication,
  deadlines, bounded retries, and stable error mapping.
- [x] Keep local and remote providers selectable during migration.
- [x] Add lease renewal or reacquisition behavior for long-running processes.
- [x] Release leases on graceful shutdown and rely on expiry after crashes.
- [x] Add integration tests using a fake account-service server.

Provider selection is deployment controlled. Local remains the default. A
remote canary sets:

```text
SEKAI_ACCOUNT_PROVIDER=remote
SEKAI_ACCOUNT_SERVICE_URL=https://accounts.example.com
SEKAI_ACCOUNT_SERVICE_TOKEN=<lease-scoped token>
SEKAI_ACCOUNT_SERVICE_TIMEOUT=10
SEKAI_ACCOUNT_SERVICE_MAX_ATTEMPTS=3
SEKAI_ACCOUNT_LEASE_STATE_DIR=/root/sekai-client/.runtime/account-leases
```

Plain HTTP is accepted only for loopback fake-service and local integration
tests. Remote responses are converted into client-owned credential models;
game authentication and session establishment remain in this repository.
The shared client reuses its current live lease across relogins, reacquires only
after expiry, and performs a credential-safe best-effort release during normal
worker shutdown. Abrupt termination remains covered by server-side expiry.
Remote acquire and release intent is journaled with atomic `0600` writes. A
restart reuses the same acquire idempotency key or completes an ambiguous
release before requesting another account. Gunicorn's `worker_exit` hook runs
the graceful release path without replacing Gunicorn's signal handlers.

Acceptance criteria:

- Scripts obtain accounts only through `AccountProvider`.
- Account-service outages do not erase a currently committed client session.
- A rejected or expired lease produces a clear degraded lifecycle state.

### Phase 5: Rollout and Remove Local Registration

The repository contains a dedicated TW remote-provider PM2 template and
[canary runbook](account-service-tw-canary.md). This is preparation only; the
production canary remains unchecked until its evidence is recorded.

- [x] Deploy the account service before enabling remote acquisition in clients.
- [x] Canary one region and one consumer, then expand by region.
- [ ] Monitor lease conflicts, acquisition latency, authentication failures,
  quarantine rate, and account inventory.
- [ ] Migrate existing credentials through an audited one-time import.
- [ ] Remove local account registration, JWT parsing, and YAML writes after the
  rollback window.
- [ ] Remove migrated game credentials from process environments and deployment
  templates.

Acceptance criteria:

- All production consumers use the remote provider.
- No game account credential remains in this repository or ordinary deployment
  configuration.
- Rollback and credential revocation procedures have been exercised.

### Phase 6: Continue Application Decomposition

- [ ] Separate update fetching/transformation from Git publication and recovery.
- [ ] Replace direct JSON-RPC method-name dependencies with narrow ranking,
  master-data, and profile interfaces.
- [ ] Separate public game APIs from the dashboard/PM2 control plane.
- [ ] Replace import-time regional globals with validated application settings.
- [ ] Move modules into a package layout after behavioral boundaries are covered
  by tests.

## Relationship to the Existing Remediation Roadmap

Before production migration, complete or explicitly accept the remaining items
in [remediation-roadmap.md](remediation-roadmap.md):

- Phase 0 production topology and repository-state verification (completed
  2026-08-13).
- Phase 1 required CI enforcement. The repository is intended to become public;
  public-release hardening and ruleset activation complete this prerequisite.
- Phase 3 desktop/mobile dashboard acceptance.
- Phase 5 PM2/Gunicorn, canary, public endpoint, and monitoring acceptance.
- Phase 6 end-to-end RPC deadlines, cancellation, idempotent retry policy, and
  queue metrics. This is a prerequisite for the remote provider.
- Phase 7 event-tracker outbox and upstream response-schema validation.

Do not combine the account-service extraction with unfinished retry, lifecycle,
or event-delivery changes in one pull request.

## Recommended Execution Order

Do not wait for every remediation phase before starting local architecture work.
Use the following order so production facts and reliability constraints shape
the extraction without blocking independent work indefinitely.

1. Complete the read-only production and repository audit from remediation
   Phase 0, then confirm the required CI check from Phase 1.
2. Complete the critical parts of remediation Phase 6: end-to-end RPC deadlines,
   abandoned-call state protection, idempotency-aware retries, queue rejection,
   and baseline queue metrics.
3. Implement account-decoupling Phases 0-2: account contracts,
   `LocalAccountProvider`, and separation of protocol/authentication/registration.
4. Before enabling the remote provider in production, complete remediation
   Phase 5 production acceptance: PM2/Gunicorn validation, one-region canary,
   public readiness checks, and monitoring.
5. Build and integrate the remote account service, then roll it out by consumer
   and region.

Remediation Phase 3 browser acceptance and Phase 7 event-tracker durability can
run in parallel. They do not block the local `AccountProvider` refactor, but
Phase 7 should be completed before broad production migration when practical so
account-source migration and event-delivery reliability are not changed at the
same time.

The Phase 0 production audit, public-release gate, critical remediation Phase 6
reliability work, and remote-provider lifecycle handling are complete. The
immediate next action is one-region canary configuration without removing the
local rollback path.

TW canary evidence (2026-08-16): the account service is deployed on Kubernetes
with Postgres and separate encryption-key management. The TW shared client runs
`main@f3f2933` from an isolated worktree with one Gunicorn worker, remote account
acquisition, and a private persistent lease journal. It reached `READY`, held
exactly one TW lease, exposed the existing loopback RPC contract to the legacy
check-update client, and contained no legacy TW credential variables. The 06:00
JST scheduled update completed and pushed the TW repository successfully without
process restarts or new error-log entries. The 05:30 failure was traced to stale
PAT-based Git process configuration and missing dynamic repositories; reloading
the update workers with GitHub App authentication and restoring the repositories
resolved it. Broader regional rollout and sustained monitoring remain pending.

## Suggested Pull Requests

| PR | Conventional title | Scope |
|---|---|---|
| 1 | `refactor: introduce account provider contract` | Models, protocol, contract tests |
| 2 | `refactor: isolate local account provider` | Move YAML/env handling out of shared client |
| 3 | `refactor: separate protocol authentication` | Transport, session, registration adapter |
| 4 | `feat: add remote account provider` | HTTP client and integration tests |
| 5 | `chore: migrate account acquisition service` | Canary configuration and migration tooling |
| 6 | `refactor: remove local account registration` | Delete legacy registration and credential files |

The separate repository should use its own small, independently deployable pull
requests for schema, lease operations, registration worker, API security, and
operations readiness.
