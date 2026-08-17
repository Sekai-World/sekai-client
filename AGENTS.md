# Repository Agent Instructions

This repository is public. Before committing or pushing documentation, redact
production evidence by default.

- Do not publish credentials, tokens, account or lease identifiers, internal
  endpoints or hostnames, filesystem paths, image digests, deployment commit
  identifiers, exact timestamps, or detailed operational counters.
- Public documents may state aggregate health, rollout decisions, acceptance
  gates, and rollback requirements without identifying infrastructure.
- Keep exact runtime evidence in a private operator record and reference it
  only as private evidence in public roadmaps.
- Review the final diff for sensitive values before pushing or opening a PR.
