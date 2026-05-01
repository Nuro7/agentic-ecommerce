"""Unit of Work port interface.

Wraps a database transaction so use cases can commit or rollback without
knowing the ORM. Implemented by SQLAlchemy UoW in infrastructure.

Populated in: Module 2 — Tenant core and data isolation.
"""
