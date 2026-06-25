#!/bin/bash
set -e
cd ~/impresora-server
git pull
docker compose build --no-cache
docker compose up -d
echo "✅ Deploy complete"
