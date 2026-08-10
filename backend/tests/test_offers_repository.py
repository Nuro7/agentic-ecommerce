"""Repository tests for offers — active-period + redemption logic.

Regression: offers created WITHOUT explicit starts_at/ends_at (the default via
the API) must still be returned as active. The old filter did
``starts_at <= now`` which evaluates NULL and silently drops the row.
"""
import pytest
from datetime import datetime, timedelta, timezone

from src.app.modules.offers.repository import OfferRepository
from src.app.modules.offers.models import ProductOffer


@pytest.fixture(autouse=True)
async def _clean_offers(db):
    """In-memory SQLite is session-scoped — clear rows between tests."""
    from sqlalchemy import delete
    await db.execute(delete(ProductOffer))
    await db.commit()
    yield


async def _mk_offer(db, tenant_id="t1", **kw):
    data = {
        "tenant_id": tenant_id,
        "title": kw.get("title", "Test offer"),
        "offer_kind": kw.get("offer_kind", "discount"),
        "platform_id": kw.get("platform_id", "101"),
        "discount_percent": kw.get("discount_percent"),
        "discount_amount": kw.get("discount_amount"),
        "starts_at": kw.get("starts_at"),
        "ends_at": kw.get("ends_at"),
        "is_active": kw.get("is_active", True),
        "max_redemptions": kw.get("max_redemptions"),
        "redemption_count": kw.get("redemption_count", 0),
    }
    offer = ProductOffer(**{k: v for k, v in data.items() if v is not None or k in ("starts_at", "ends_at")})
    db.add(offer)
    await db.commit()
    await db.refresh(offer)
    return offer


@pytest.mark.asyncio
async def test_offer_without_dates_is_active(db):
    await _mk_offer(db, title="No dates")
    offers = await OfferRepository(db).get_active_offers("t1")
    assert len(offers) == 1
    assert offers[0].title == "No dates"


@pytest.mark.asyncio
async def test_offer_inside_window_is_active(db):
    now = datetime.now(timezone.utc)
    await _mk_offer(
        db,
        title="In window",
        starts_at=now - timedelta(days=1),
        ends_at=now + timedelta(days=1),
    )
    offers = await OfferRepository(db).get_active_offers("t1")
    assert len(offers) == 1


@pytest.mark.asyncio
async def test_expired_offer_not_active(db):
    now = datetime.now(timezone.utc)
    await _mk_offer(
        db,
        title="Expired",
        starts_at=now - timedelta(days=2),
        ends_at=now - timedelta(days=1),
    )
    offers = await OfferRepository(db).get_active_offers("t1")
    assert offers == []


@pytest.mark.asyncio
async def test_inactive_offer_excluded(db):
    await _mk_offer(db, title="Disabled", is_active=False)
    offers = await OfferRepository(db).get_active_offers("t1")
    assert offers == []


@pytest.mark.asyncio
async def test_exhausted_offer_excluded(db):
    await _mk_offer(db, title="Exhausted", max_redemptions=2, redemption_count=2)
    offers = await OfferRepository(db).get_active_offers("t1")
    assert offers == []


@pytest.mark.asyncio
async def test_kind_filter(db):
    await _mk_offer(db, title="Bulk offer", offer_kind="bulk")
    await _mk_offer(db, title="Discount offer", offer_kind="discount")
    offers = await OfferRepository(db).get_active_offers("t1", kinds=["bulk"])
    assert len(offers) == 1
    assert offers[0].offer_kind == "bulk"


@pytest.mark.asyncio
async def test_tenant_isolation(db):
    await _mk_offer(db, tenant_id="t1", title="t1 offer")
    await _mk_offer(db, tenant_id="t2", title="t2 offer")
    offers = await OfferRepository(db).get_active_offers("t1")
    assert [o.title for o in offers] == ["t1 offer"]


@pytest.mark.asyncio
async def test_increment_redemptions(db):
    await _mk_offer(db, title="Counter", max_redemptions=5)
    offers = await OfferRepository(db).get_active_offers("t1")
    offer_id = offers[0].id
    await OfferRepository(db).increment_redemptions(offer_id, "t1")
    after = await OfferRepository(db).get_by_id(offer_id, "t1")
    assert after.redemption_count == 1
