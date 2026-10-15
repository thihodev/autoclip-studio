#!/usr/bin/env bash
# Clones and starts the two open-source engines AutoClip Studio drives:
#   - MoneyPrinterTurbo (render): http://127.0.0.1:8080
#   - Postiz (publishing):        http://localhost:4007
# Requires Docker (Docker Desktop / OrbStack on macOS).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p vendor

command -v docker >/dev/null || { echo "Docker is required: https://www.docker.com/products/docker-desktop"; exit 1; }

# ---- MoneyPrinterTurbo ----
if [ ! -d vendor/MoneyPrinterTurbo ]; then
  git clone --depth 1 https://github.com/harry0703/MoneyPrinterTurbo.git vendor/MoneyPrinterTurbo
fi
if [ ! -f vendor/MoneyPrinterTurbo/config.toml ]; then
  cp vendor/MoneyPrinterTurbo/config.example.toml vendor/MoneyPrinterTurbo/config.toml
  echo ">> Edit vendor/MoneyPrinterTurbo/config.toml and set pexels_api_keys (free: https://www.pexels.com/api/)"
fi
(cd vendor/MoneyPrinterTurbo && docker compose up -d)

# ---- Postiz ----
if [ ! -d vendor/postiz ]; then
  mkdir -p vendor/postiz
  curl -fsSL https://raw.githubusercontent.com/gitroomhq/postiz-docker-compose/main/docker-compose.yaml \
    -o vendor/postiz/docker-compose.yaml
  echo ">> Review vendor/postiz/docker-compose.yaml: set JWT_SECRET and the TikTok/YouTube/Facebook app keys"
  echo "   (docs: https://docs.postiz.com/providers)"
fi
(cd vendor/postiz && docker compose up -d)

echo
echo "MoneyPrinterTurbo API: http://127.0.0.1:8080/docs"
echo "Postiz UI:             http://localhost:4007  (connect channels, then Settings → Public API → copy key into .env)"
