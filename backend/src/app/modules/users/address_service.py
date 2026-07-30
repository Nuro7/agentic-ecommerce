"""Address service for saved address lookup."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.database import AsyncSessionLocal
from ...modules.users.models import SavedAddress

logger = logging.getLogger(__name__)


async def save_address(
    session_id: str,
    tenant_id: str,
    phone: str,
    address_data: Dict[str, Any],
) -> None:
    """Save or update address for a session (upsert by session_id)."""
    async with AsyncSessionLocal() as db:
        # Check if address already exists for this session
        result = await db.execute(
            select(SavedAddress).where(
                SavedAddress.session_id == session_id,
                SavedAddress.tenant_id == tenant_id,
            )
        )
        existing = result.scalar_one_or_none()
        
        if existing:
            # Update existing
            for key, value in address_data.items():
                if hasattr(existing, key) and value is not None:
                    setattr(existing, key, str(value))
            existing.phone = phone
        else:
            # Create new
            addr = SavedAddress(
                session_id=session_id,
                tenant_id=tenant_id,
                phone=phone,
                first_name=address_data.get("first_name"),
                last_name=address_data.get("last_name"),
                address_line1=address_data.get("address_1") or address_data.get("address_line1"),
                city=address_data.get("city"),
                state=address_data.get("state"),
                postcode=address_data.get("postcode"),
                email=address_data.get("email"),
            )
            db.add(addr)
        await db.commit()


async def get_address_by_phone(
    phone: str,
    tenant_id: str,
) -> Optional[Dict[str, Any]]:
    """Look up saved address by phone number."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(SavedAddress).where(
                SavedAddress.phone == phone,
                SavedAddress.tenant_id == tenant_id,
            ).order_by(SavedAddress.updated_at.desc())
        )
        addr = result.scalar_one_or_none()
        if not addr:
            return None
        return {
            "first_name": addr.first_name or "",
            "last_name": addr.last_name or "",
            "address_1": addr.address_line1 or "",
            "city": addr.city or "",
            "state": addr.state or "",
            "postcode": addr.postcode or "",
            "phone": addr.phone or "",
            "email": addr.email or "",
            "country": "IN",
        }


async def get_address_by_session(
    session_id: str,
    tenant_id: str,
) -> Optional[Dict[str, Any]]:
    """Return latest saved address for a session."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(SavedAddress).where(
                SavedAddress.session_id == session_id,
                SavedAddress.tenant_id == tenant_id,
            ).order_by(SavedAddress.updated_at.desc())
        )
        addr = result.scalar_one_or_none()
        if not addr:
            return None
        return {
            "first_name": addr.first_name or "",
            "last_name": addr.last_name or "",
            "address_1": addr.address_line1 or "",
            "city": addr.city or "",
            "state": addr.state or "",
            "postcode": addr.postcode or "",
            "phone": addr.phone or "",
            "email": addr.email or "",
            "country": "IN",
        }