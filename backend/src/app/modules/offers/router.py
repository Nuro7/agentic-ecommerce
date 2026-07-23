from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from ...core.database import get_db
from ..auth.dependencies import get_current_tenant_id
from .schemas import ProductOfferCreate, ProductOfferUpdate, ProductOfferOut
from .dependencies import get_offer_service
from .service import OfferService

router = APIRouter(prefix="/offers", tags=["offers"])


@router.get("/", response_model=List[ProductOfferOut])
async def list_offers(
    tenant_id: str = Depends(get_current_tenant_id),
    service: OfferService = Depends(get_offer_service),
):
    return await service.list_offers(tenant_id)


@router.post("/", response_model=ProductOfferOut, status_code=status.HTTP_201_CREATED)
async def create_offer(
    body: ProductOfferCreate,
    tenant_id: str = Depends(get_current_tenant_id),
    service: OfferService = Depends(get_offer_service),
):
    return await service.create_offer(tenant_id, body.model_dump())


@router.put("/{offer_id}", response_model=ProductOfferOut)
async def update_offer(
    offer_id: str,
    body: ProductOfferUpdate,
    tenant_id: str = Depends(get_current_tenant_id),
    service: OfferService = Depends(get_offer_service),
):
    result = await service.update_offer(offer_id, tenant_id, body.model_dump(exclude_none=True))
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Offer not found")
    return result


@router.delete("/{offer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_offer(
    offer_id: str,
    tenant_id: str = Depends(get_current_tenant_id),
    service: OfferService = Depends(get_offer_service),
):
    deleted = await service.delete_offer(offer_id, tenant_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Offer not found")


@router.get("/active", response_model=List[ProductOfferOut])
async def get_active_promotions(
    tenant_id: str = Depends(get_current_tenant_id),
    service: OfferService = Depends(get_offer_service),
):
    return await service.get_active_promotions(tenant_id)
