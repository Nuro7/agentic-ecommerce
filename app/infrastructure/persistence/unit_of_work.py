"""SQLAlchemy Unit of Work implementation.

Implements the UnitOfWork port using AsyncSession. Exposes commit
and rollback around a single database transaction.

Populated in: Module 2 — Tenant core and data isolation.
"""
