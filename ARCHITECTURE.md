# Architecture overview

## Layers

This codebase follows clean architecture. Each layer has clear
responsibilities and import constraints.

### Ring 1 — `app/domain/`

Pure business entities and value objects. Imports allowed: standard
library, `pydantic`. Forbidden: `sqlalchemy`, `fastapi`, `httpx`, any
third-party SDK.

### Ring 2 — `app/application/`

Use cases that orchestrate domain entities through port interfaces.
Imports allowed: `app.domain`, standard library, `pydantic`.
Forbidden: anything from `app.infrastructure` or `app.interfaces`.

### Ring 3 — `app/infrastructure/`

Concrete implementations of ports defined in `app/application/ports/`.
SQLAlchemy models, Redis clients, third-party SDK clients live here.
Imports allowed: anything.

### Ring 4 — `app/interfaces/`

HTTP routers, WebSocket handlers, CLI commands, background workers.
Thin translation layers — parse input, build commands, call use cases,
format responses. Imports allowed: anything.

### Cross-cutting — `app/core/`

Configuration, logging, error hierarchy, DI container, context vars.
Imported by all rings.

## Migration from legacy code

Existing code in `wooagent-backend/` and `wooagent/` will be migrated
into the new structure module by module. Until migrated, the legacy
code keeps running and the new `app/` is built alongside.

Migration path:
- `wooagent-backend/agent/` → `app/application/conversation/` +
  `app/domain/conversation/` (Module 5)
- `wooagent-backend/services/woocommerce.py` →
  `app/infrastructure/adapters/woocommerce/` (Module 7)
- `wooagent-backend/routers/live.py` → `app/interfaces/websocket/live.py`
  + `app/infrastructure/llm/gemini_live.py` (Module 6)
- `wooagent/` → `frontends/wordpress-plugin/` (Module 7)
