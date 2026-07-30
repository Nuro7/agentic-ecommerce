#!/bin/bash
set -e

# Speako production deploy script
# Called by Drone CI via SSH. Stops old containers before starting new ones
# to avoid "container name already in use" conflicts.

cd ~/agentic-ecommerce

echo "[deploy] Stopping existing containers..."
docker compose -f infra/docker/docker-compose.prod.yml down --remove-orphans 2>/dev/null || true
# Also remove orphan networks that may linger
docker network prune -f 2>/dev/null || true

echo "[deploy] Building and starting services..."
# --force-recreate ensures containers are recreated even if names conflict
# from stale state or simultaneous CI runs
docker compose -f infra/docker/docker-compose.prod.yml up -d --build --force-recreate

echo "[deploy] Waiting for services to settle..."
sleep 10

echo "[deploy] Service status:"
docker compose -f infra/docker/docker-compose.prod.yml ps

echo "[deploy] Done."
