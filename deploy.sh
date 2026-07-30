#!/bin/bash
set -e

# Speako production deploy script
# Called by Drone CI via SSH. Stops old containers before starting new ones
# to avoid "container name already in use" conflicts.

cd ~/agentic-ecommerce

echo "[deploy] Stopping existing containers..."
docker compose -f infra/docker/docker-compose.prod.yml down --remove-orphans

echo "[deploy] Building and starting services..."
docker compose -f infra/docker/docker-compose.prod.yml up -d --build

echo "[deploy] Waiting for web to be healthy..."
sleep 10
docker compose -f infra/docker/docker-compose.prod.yml ps

echo "[deploy] Done."
