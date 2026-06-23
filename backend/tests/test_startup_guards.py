"""Startup boot guards in server.py lifespan.

- `_assert_single_process_or_acked` (P2-11/P2-12): refuse multi-process while the voice
  cap and circuit breakers are per-process, unless SPEAKO_ALLOW_MULTI_PROCESS is set.
- `_enforce_rls_role` (RLS-role guard): refuse to boot if the DB role bypasses RLS while
  RLS is enabled, unless SPEAKO_ALLOW_RLS_BYPASS is set. (Live-Postgres behaviour is
  verified against a throwaway PG: agentic superuser raises, rls_test (f,f) passes.)
"""
import pytest

from src.app.server import _assert_single_process_or_acked, _enforce_rls_role

_GUARD_ENVS = (
    "WEB_CONCURRENCY", "SPEAKO_WEB_REPLICAS",
    "SPEAKO_ALLOW_MULTI_PROCESS", "SPEAKO_ALLOW_RLS_BYPASS",
)


def _clear(monkeypatch):
    for k in _GUARD_ENVS:
        monkeypatch.delenv(k, raising=False)


# ── P2-11/P2-12: multi-process scale-guard ────────────────────────────────────

def test_single_process_boots(monkeypatch):
    _clear(monkeypatch)
    _assert_single_process_or_acked()  # no raise


def test_explicit_one_boots(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    _assert_single_process_or_acked()  # no raise


def test_multi_worker_refuses_to_boot(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(RuntimeError):
        _assert_single_process_or_acked()


def test_multi_replica_refuses_to_boot(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SPEAKO_WEB_REPLICAS", "3")
    with pytest.raises(RuntimeError):
        _assert_single_process_or_acked()


def test_ack_flag_allows_multi_process(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    monkeypatch.setenv("SPEAKO_ALLOW_MULTI_PROCESS", "true")
    _assert_single_process_or_acked()  # no raise — operator acknowledged


# ── RLS-role guard ────────────────────────────────────────────────────────────

def test_safe_role_passes(monkeypatch):
    _clear(monkeypatch)
    _enforce_rls_role("app", rolsuper=False, rolbypassrls=False, rls_enabled=True)  # (f,f) → ok


def test_superuser_with_rls_on_refuses_to_boot(monkeypatch):
    _clear(monkeypatch)
    with pytest.raises(RuntimeError):
        _enforce_rls_role("agentic", rolsuper=True, rolbypassrls=True, rls_enabled=True)


def test_bypassrls_only_with_rls_on_refuses_to_boot(monkeypatch):
    _clear(monkeypatch)
    with pytest.raises(RuntimeError):
        _enforce_rls_role("app", rolsuper=False, rolbypassrls=True, rls_enabled=True)


def test_superuser_but_rls_not_enabled_warns_not_raises(monkeypatch):
    # Local dev: agentic is a superuser but 0013 isn't applied → must still boot.
    _clear(monkeypatch)
    _enforce_rls_role("agentic", rolsuper=True, rolbypassrls=True, rls_enabled=False)  # no raise


def test_override_flag_allows_bypass_role(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SPEAKO_ALLOW_RLS_BYPASS", "true")
    _enforce_rls_role("agentic", rolsuper=True, rolbypassrls=True, rls_enabled=True)  # no raise
