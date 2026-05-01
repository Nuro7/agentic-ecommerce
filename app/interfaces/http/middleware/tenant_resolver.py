"""Tenant resolver middleware.

Extracts tenant identity from JWT, API key, or subdomain and binds
tenant_id to the request ContextVar.

Populated in: Module 2 — Tenant core and data isolation.
"""
