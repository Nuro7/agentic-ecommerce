"""Redis implementation of the CachePort.

Uses aioredis for async get/set/delete/exists operations with
tenant-namespaced keys to enforce isolation.

Populated in: Task 1.5 — Infrastructure layer skeleton.
"""
