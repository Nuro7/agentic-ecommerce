"""Rate limiting middleware.

Enforces per-tenant request rate limits using a Redis sliding window
counter, returning HTTP 429 when exceeded.

Populated in: Module 4 — Billing and usage metering.
"""
