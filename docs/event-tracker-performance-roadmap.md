# Event Tracker Performance Roadmap

## Objective

Reduce event-ranking collection and delivery latency without weakening client
state isolation, delivery durability, or upstream rate-limit safety.

Measure three separate outcomes:

- **collection latency**: scheduler start to a complete ranking snapshot;
- **durable latency**: scheduler start to committing the snapshot to the outbox;
- **delivery latency**: scheduler start to confirmed storage by the remote API.

Writing to an outbox quickly does not satisfy the end-to-end delivery target.

## Production Baseline

The baseline below covers 480 scheduled cycles per region during the 24 hours
ending 2026-08-17 JST. JP had no active event and skipped collection.

| Region | Active samples | Mean | Median | P95 | Maximum |
| --- | ---: | ---: | ---: | ---: | ---: |
| EN | 479 | 20.0 s | 18 s | 31 s | 64 s |
| KR | 480 | 11.1 s | 10 s | 19 s | 58 s |
| TW | 479 | 11.5 s | 10 s | 20.1 s | 61 s |

Time to the normal-ranking POST averaged 16.3 seconds for EN, 5.9 seconds for
KR, and 6.3 seconds for TW. World Link processing and chapter delivery added an
average of 3.7-5.2 seconds.

Current scheduler success is not reliable delivery evidence: POST failures are
logged and swallowed, so a job may be reported as successful without confirmed
remote storage.

## Targets

- Initial target: KR and TW delivery mean at or below 5 seconds, with P95 below
  10 seconds.
- EN target: establish a measured network and upstream latency budget before
  committing to 5 seconds.
- Stretch target: delivery below 1 second only if measurements demonstrate that
  upstream game requests can meet the budget. It is not considered feasible for
  the current polling design by default.
- No regression in snapshot durability, idempotency, account lifecycle safety,
  or upstream request volume.

## Phase 0: Instrument the Critical Path

- [ ] Record durations for version check, account lookup, first-100 fetch,
  border fetch, normal-ranking delivery, metadata lookup, and chapter delivery.
- [ ] Record collection, durable, and confirmed-delivery latency separately.
- [ ] Add counters for collection failures, delivery failures, retries, scheduler
  skips, and API status classes.
- [ ] Establish 24-hour mean, median, P95, P99, and maximum baselines by region.
- [ ] Confirm the receiving API can identify when a payload is durably stored.

Acceptance: dashboards and logs can locate the dominant stage without exposing
account, ranking, or authentication data.

## Phase 1: Reliable Delivery Semantics

This phase shares the Event Tracker outbox scope in
[remediation-roadmap.md](remediation-roadmap.md).

- [x] Persist each snapshot to a local SQLite outbox before delivery.
- [x] Use an idempotency key based on region, event, collection timestamp, and
  data type.
- [x] Deliver independently with bounded retries and exponential backoff.
- [x] Bound each scheduler drain to 30 seconds and each delivery request to 15
  seconds so a slow remote API cannot monopolize the three-minute schedule.
- [x] Mark local delivery complete only after the receiving API confirms HTTP
  success; receiver-side idempotency enforcement remains pending.
- [x] Resume pending deliveries after process restart.
- [x] Prune terminal `sent` and `failed` rows after the configurable 24-hour
  retention window while preserving pending and sending records.
- [x] Publish outbox depth, oldest-item age, and delivery-attempt state in logs;
  metrics endpoint integration remains part of Phase 0.

Acceptance: scheduler completion, snapshot durability, and remote delivery have
distinct states; a transient API failure cannot lose a snapshot.

## Phase 2: Remove Avoidable Transport Cost

- [ ] Reuse bounded `requests.Session` connection pools for internal RPC and
  external HTTPS requests.
- [ ] Cache World Bloom metadata and refresh it on event/version change or a
  bounded TTL instead of downloading it every cycle.
- [ ] Add a receiving API endpoint that accepts normal and chapter rankings in
  one idempotent request where their lifecycle permits atomic delivery.
- [ ] Measure DNS, connect, TLS, server, and response time before and after the
  changes.

Acceptance: request count and connection setup fall without stale metadata or
cross-region session state.

## Phase 3: Reduce Collection Latency

- [ ] Add one combined shared-client RPC operation for a complete event snapshot
  to avoid queue interleaving and repeated local RPC round trips.
- [ ] Determine whether first-100 and border game requests can safely overlap.
- [ ] Do not add concurrency to the existing stateful single-client worker
  without protocol, rate-limit, and state-isolation evidence.
- [ ] If concurrency is required, evaluate isolated clients and accounts rather
  than sharing one mutable game session.
- [ ] Measure whether EN latency is caused by upstream response time, network
  route, account behavior, or receiving API placement.
- [ ] Evaluate an EN worker location change only after controlled route tests.

Acceptance: KR/TW meet the 5-second mean target under a real event, with no
increase in authentication failures, throttling, queue rejection, or lease
churn. EN receives a separate evidence-backed target.

## Phase 4: Canary and Rollout

- [ ] Benchmark changes locally with deterministic delayed and failed endpoints.
- [ ] Canary one active region while retaining the current deployment rollback.
- [ ] Observe at least 24 hours and an event-boundary period.
- [ ] Compare mean/P95/P99 latency, delivery failures, retry backlog, request
  volume, account health, and process restarts against this baseline.
- [ ] Roll out one region at a time only when reliability gates remain satisfied.

Rollback triggers include lost or duplicate snapshots, sustained outbox growth,
authentication or rate-limit growth, readiness degradation, and latency P95
worse than the baseline.

## Recommended Execution Order

1. Instrument the current path.
2. Implement durable outbox and truthful delivery states.
3. Reuse connections, cache metadata, and batch delivery.
4. Re-baseline before considering collection concurrency.
5. Treat sub-second delivery as a separate architecture decision, potentially
   requiring upstream push or continuously maintained data rather than polling.
