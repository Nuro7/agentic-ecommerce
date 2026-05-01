# ADR 0001 — Adopt clean architecture with four rings

Date: 2026-05-01
Status: Accepted

## Context

The system must support multiple e-commerce platforms (WordPress, Shopify,
custom) with a single backend, and must be safely modifiable by AI-assisted
development without architectural drift.

## Decision

Adopt clean architecture with four concentric rings: domain, application,
infrastructure, interfaces. Enforce import direction with import-linter
in CI.

## Consequences

Positive:
- Adding a platform is one new adapter in infrastructure, zero changes to
  domain/application.
- Tests can run against in-memory implementations without Postgres or Redis.
- Business logic survives framework changes.

Negative:
- More files than a flat layout. Boilerplate when adding a feature.
- Mappers between ORM models and domain entities feel redundant initially.

## Alternatives considered

- Flat structure (everything in services/): rejected — has been tried in
  legacy `wooagent-backend/`, leads to god-objects.
- Hexagonal architecture: similar benefits, slightly different terminology.
  Clean architecture is more familiar to AI-assisted tooling.
