# Event Tracker Performance Roadmap

## Status

**Implementation complete; production performance acceptance pending.**

The client-side collection and durable-delivery changes are implemented. The
receiving API now persists and enforces idempotency keys for ranking writes.
The same implementation is deployed for EN, JP, KR, and TW; production
acceptance remains pending for the full fleet.
Unchecked items below are production acceptance work or optional follow-up
optimizations, not blockers for implementation completion.

## Objective

Reduce event-ranking collection and delivery latency without weakening client
state isolation, delivery durability, or upstream rate-limit safety.

Measure three separate outcomes:

- **collection latency**: scheduler start to a complete ranking snapshot;
- **durable latency**: scheduler start to committing the snapshot to the outbox;
- **delivery latency**: scheduler start to confirmed storage by the remote API.

Writing to an outbox quickly does not satisfy the end-to-end delivery target.

## Production Baseline

The initial baseline was collected during a 24-hour production observation
window. Exact sample counts, timestamps, and latency values remain in the
private operator record; this public roadmap retains only the decision-level
comparison.

| Region | Public baseline status |
| --- | --- |
| EN | Active event observed; slower than the initial KR/TW target |
| KR | Active event observed; used as a target-region comparison |
| TW | Active event observed; used as a target-region comparison |
| JP | No active event during the observation window |

Detailed ranking POST and World Link timing remains private and is used only for
the acceptance comparison.

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

- [x] Record durations for version check, the combined game snapshot, durable
  enqueue, delivery, and total execution. The redundant account lookup was
  removed; receiver-side timing remains external.
- [x] Record collection, durable, and confirmed-HTTP-delivery latency separately.
- [x] Add counters for collection failures, delivery failures, retries, scheduler
  skips, and API status classes.
- [x] Establish the initial 24-hour mean, median, P95, and maximum baseline by
  region. P99 will be added to the post-deployment comparison.
- [x] Confirm the receiving API can identify when a payload is durably stored.

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
  success; the deployed receiver persists and enforces idempotency keys.
- [x] Resume pending deliveries after process restart.
- [x] Prune terminal `sent` and `failed` rows after the configurable 24-hour
  retention window while preserving pending and sending records.
- [x] Publish outbox depth, oldest-item age, and delivery-attempt state in logs;
  metrics endpoint integration remains part of Phase 0.

Acceptance: scheduler completion, snapshot durability, and remote delivery have
distinct states; a transient API failure cannot lose a snapshot.

## Phase 2: Remove Avoidable Transport Cost

- [x] Reuse bounded `requests.Session` connection pools for internal RPC and
  external HTTPS requests.
- [x] Cache World Bloom metadata and refresh it on event/version change or a
  bounded TTL instead of downloading it every cycle.
- [ ] Add a receiving API endpoint that accepts normal and chapter rankings in
  one idempotent request where their lifecycle permits atomic delivery.
- [ ] Measure DNS, connect, TLS, server, and response time before and after the
  changes.

Acceptance: request count and connection setup fall without stale metadata or
cross-region session state.

## Phase 3: Reduce Collection Latency

- [x] Add one combined shared-client RPC operation for a complete event snapshot
  to avoid queue interleaving and repeated local RPC round trips.
- [x] Determine whether first-100 and border game requests can safely overlap:
  they must remain serialized while using one mutable authenticated client.
- [x] Do not add concurrency to the existing stateful single-client worker
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

## Production Acceptance Procedure

Keep exact timestamps, identifiers, endpoints, sample counts, latency values,
and operational counters in the private operator record. Record only the final
gate decision and aggregate status in this public roadmap.

The implementation is already deployed to EN, JP, KR, and TW. Treat the first
successful cycle after all regional dependencies became healthy as the start of
the full-fleet observation window; deployment-transition failures are retained
in the private record but do not count as steady-state samples.

1. Verify rollback artifacts, readiness, scheduler execution, outbox creation,
   and successful idempotent delivery for every region.
2. Observe all four regions for at least 24 hours and across an event boundary.
   Collect per-cycle collection, durable, and confirmed-delivery latency,
   including mean, P95, P99, and maximum for each region.
3. Compare delivery failures, retries, outbox depth and oldest-item age,
   scheduler skips, request volume, authentication/rate-limit signals, and
   process restarts with the baseline.
4. Pass KR/TW only when delivery mean is at most 5 seconds, P95 is below 10
   seconds, no snapshot is lost or duplicated, backlog does not grow
   persistently, and reliability signals do not regress.
5. Establish EN's network and upstream latency budget before assigning its
   latency threshold. Establish JP's first active-event baseline because the
   initial observation window had no active JP event. Both regions must still
   pass the common durability and reliability gates.
6. Roll back an affected region on any trigger above. Otherwise record the
   private evidence reference and one public pass/fail decision per region.

## Recommended Execution Order

1. Instrument the current path.
2. Implement durable outbox and truthful delivery states.
3. Reuse connections, cache metadata, and batch delivery.
4. Re-baseline before considering collection concurrency.
5. Treat sub-second delivery as a separate architecture decision, potentially
   requiring upstream push or continuously maintained data rather than polling.
