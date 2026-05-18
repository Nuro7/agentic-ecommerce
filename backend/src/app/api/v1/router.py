"""API v1 — mounts all module routers."""
from fastapi import APIRouter
from ...modules.tenants.router import router as tenants_router
from ...modules.auth.router import router as auth_router
from ...modules.auth.oauth.shopify import router as shopify_router
from ...modules.users.router import router as users_router
from ...modules.billing.router import router as billing_router
from ...modules.products.router import router as products_router
from ...modules.conversations.router import router as conversations_router
from ...modules.carts.router import router as carts_router
from ...modules.orders.router import router as orders_router
from ...modules.webhooks.router import router as webhooks_router
from ...modules.analytics.router import router as analytics_router
from .health import router as health_router
from .chat import router as chat_router
from .public import router as public_router

# voice_router is intentionally NOT included here — it is mounted at root in server.py
# so the WebSocket lives at /wooagent/stream, not /api/v1/wooagent/stream

api_router = APIRouter()

api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(shopify_router)
api_router.include_router(tenants_router)
api_router.include_router(users_router)
api_router.include_router(billing_router)
api_router.include_router(products_router)
api_router.include_router(conversations_router)
api_router.include_router(carts_router)
api_router.include_router(orders_router)
api_router.include_router(webhooks_router)
api_router.include_router(analytics_router)
api_router.include_router(chat_router)
api_router.include_router(public_router)
