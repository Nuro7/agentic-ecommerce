# ADR 0003 — Platform-agnostic CommercePort with adapter pattern

Date: 2026-05-01
Status: Accepted

## Context

The system must serve WordPress, Shopify, and custom platforms. Building
each as a separate code path would triple maintenance.

## Decision

Define one internal interface (`CommercePort`) in
`app/application/ports/commerce_port.py`. Each platform implements it as
an adapter in `app/infrastructure/adapters/`. The agent calls the port,
never the platform directly.

## Consequences

Positive:
- New platforms require one adapter file, zero changes elsewhere.
- Agent logic is testable against an in-memory fake adapter.

Negative:
- The interface must accommodate the union of platform capabilities.
  Some platforms will not support some methods — handled via a capability
  matrix.
