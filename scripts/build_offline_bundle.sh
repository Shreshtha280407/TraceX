#!/usr/bin/env sh
# Build the TraceX appliance image and package it with its compose stack into
# one tarball that installs on an air-gapped Linux host:
#
#   scripts/build_offline_bundle.sh            # -> dist/tracex-offline-<version>.tar.gz
#   (copy to the offline host)  tar xzf tracex-offline-*.tar.gz && ./tracex-offline/install.sh
set -eu
cd "$(dirname "$0")/.."
version=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)
mkdir -p dist
out=$(mktemp -d "dist/tracex-offline-${version}-XXXXXXXX")
image=${TRACEX_BUNDLE_IMAGE:-tracex-appliance:bundle-$(basename "$out")}
# TRACEX_BUILD_CA=/path/ca.crt for a TLS-inspecting proxy (build time only).
if [ -n "${TRACEX_BUILD_CA:-}" ]; then set -- --secret "id=build_ca,src=${TRACEX_BUILD_CA}"; else set --; fi
if [ -z "${TRACEX_BUNDLE_IMAGE:-}" ]; then docker build "$@" -t "$image" .; fi
docker image inspect "$image" >/dev/null
docker image inspect postgres:16-alpine >/dev/null 2>&1 || docker pull postgres:16-alpine
docker save "$image" postgres:16-alpine -o "$out/images.tar"
cp deploy/appliance/docker-compose.yml deploy/appliance/.env.example deploy/appliance/install.sh "$out/"
cp deploy/appliance/README.md "$out/README.md"
if [ "$(uname -s)" = Darwin ]; then
  sed -i '' "s|^TRACEX_IMAGE=.*|TRACEX_IMAGE=$image|" "$out/.env.example"
else
  sed -i "s|^TRACEX_IMAGE=.*|TRACEX_IMAGE=$image|" "$out/.env.example"
fi
cp pyproject.toml uv.lock frontend/package-lock.json "$out/"
python3 scripts/offline_manifest.py "$out" "$image"
(cd "$out" && if command -v sha256sum >/dev/null 2>&1; then sha256sum images.tar docker-compose.yml .env.example install.sh README.md manifest.json pyproject.toml uv.lock package-lock.json; else shasum -a 256 images.tar docker-compose.yml .env.example install.sh README.md manifest.json pyproject.toml uv.lock package-lock.json; fi > SHA256SUMS)
tar -C dist -czf "${out}.tar.gz" "$(basename "$out")"
ls -lh "${out}.tar.gz"
