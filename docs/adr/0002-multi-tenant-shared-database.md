# ADR 0002 — Multi-tenant pool model with PostgreSQL Row-Level Security

Date: 2026-05-01
Status: Accepted

## Context

Multi-tenancy can be silo (DB per tenant), pool (shared DB with tenant_id),
or bridge (shared DB, separate schemas). The choice affects every later
module.

## Decision

Pool model. All tenant data shares one PostgreSQL database with `tenant_id`
on every tenant-owned table. Isolation enforced at three layers: application
filtering, PostgreSQL Row-Level Security, Redis key namespacing.

## Consequences

Positive:
- Cheap to operate. New tenant is one INSERT.
- Easy schema migration (one DB, not N).
- RLS provides defence-in-depth at the database level.

Negative:
- Noisy-neighbour risk if one tenant generates extreme load. Mitigated by
  per-tenant rate limits and connection pool tuning.
- Contractual silo offerings (enterprise) require a separate code path.
  Deferred until needed.
