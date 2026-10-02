#!/usr/bin/env sh
# Build the TraceX appliance image and package it with its compose stack into
# one tarball that installs on an air-gapped Linux host:
#
#   scripts/build_offline_bundle.sh            # -> dist/tracex-offline-<version>.tar.gz
#   (copy to the offline host)  tar xzf tracex-offline-*.tar.gz && ./tracex-offline/install.sh
set -eu
cd "$(dirname "$0")/.."
version=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)
out=dist/tracex-offline
rm -rf "$out" && mkdir -p "$out"
# TRACEX_BUILD_CA=/path/ca.crt for a TLS-inspecting proxy (build time only).
if [ -n "${TRACEX_BUILD_CA:-}" ]; then set -- --secret "id=build_ca,src=${TRACEX_BUILD_CA}"; else set --; fi
docker build "$@" -t tracex-appliance:latest -t "tracex-appliance:${version}" .
docker image inspect postgres:16-alpine >/dev/null 2>&1 || docker pull postgres:16-alpine
docker save tracex-appliance:latest "tracex-appliance:${version}" postgres:16-alpine -o "$out/images.tar"
cp deploy/appliance/docker-compose.yml deploy/appliance/.env.example deploy/appliance/install.sh "$out/"
cp deploy/appliance/README.md "$out/README.md"
tar -C dist -czf "dist/tracex-offline-${version}.tar.gz" tracex-offline
ls -lh "dist/tracex-offline-${version}.tar.gz"
