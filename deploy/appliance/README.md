# TraceX offline appliance (Linux)

Current strict isolation uses `TRACEX_UI_INTERNAL=true`: API, worker and
PostgreSQL attach only to internal Docker networks. On native Linux Docker
Engine, use the host-private API bridge URL printed by `install.sh`; published
loopback ports may be unavailable in this mode. The UI and API share that
origin and need no browser CDN assets. Docker Desktop does not expose the
private Linux bridge to the host. Opting into `TRACEX_UI_INTERNAL=false` can
restore published-port access, but permits API egress and must never be called
whole-appliance isolation. Do not change system firewall rules or disconnect
other users' applications to hide this distinction.

A complete, offline TraceX installation: PostgreSQL, the API (which also serves
the web UI), and the ingestion/analysis worker, with the ML stack and an
open-source Geo-IP database (DB-IP IP-to-Country Lite, CC BY 4.0; IPtoASN, PDDL)
baked into the image. Nothing contacts the internet at runtime: the worker and
the database sit on a Docker network with no external route.

## Requirements

* 64-bit Linux with Docker Engine 24+ and the compose plugin
* 4 CPU / 8 GB RAM is a starting profile, not a 3M guarantee. Bounded graph
  processing can spill, but ML, embeddings and risk still need admitted global
  arrays. Unsupported allocations fail explicitly rather than disabling stages.
* Disk: images, source copies, fragments, graph/features and transient scratch.
  The bounded preflight estimates 12,000 bytes per accepted transaction plus
  512 MiB reserve **in addition to already retained source/fragments**. Check
  the actual filesystem before a large import; a simple 3x-source rule is unsafe.

## Install from the offline bundle (air-gapped host)

```sh
tar xzf tracex-offline-<version>.tar.gz
cd tracex-offline
./install.sh            # loads images.tar, writes .env with fresh secrets, starts the stack
```

Open the native-Linux private API bridge URL printed by the installer, create
an account and case, then upload a CSV / JSON / NDJSON / XML file. A published
port is not the strict-isolation access path. LAN/public exposure requires a
separately reviewed routing/auth/TLS policy and changes the isolation claim.

## Build the bundle (on a connected build machine)

```sh
scripts/build_offline_bundle.sh   # -> dist/tracex-offline-<version>.tar.gz
```

## Operations

```sh
docker compose ps                         # status
docker compose logs -f worker             # import progress, machine profile
docker compose exec api tracex-geoip status
docker compose exec api tracex-dataset inspect /data/evidence/<file> --full
docker compose down                       # stop (data volumes are kept)
```

Updating the Geo-IP database offline: copy newer DB-IP / IPtoASN CSV or `.tgz`
files into the container and run `tracex-geoip import FILE...`.

Geo-IP attribution: *IP Geolocation by DB-IP* (https://db-ip.com), CC BY 4.0;
ASN data by IPtoASN (https://iptoasn.com), PDDL 1.0.

## Reproducible local review

The 2026-10-03 copied-image install and no-egress/browser/scale evidence is in
`docs/implementation_acceptance_2026-10-03.md`. Build-time internet is allowed;
runtime API, worker and PostgreSQL isolation are tested separately. A verified
compiled Geo-IP cache can be prepared with `scripts/prepare_geoip_cache.py`;
the image verifies its SHA256SUMS and includes data editions/licences.
The current release contains no LLM/chat. Explanations are deterministic,
source-backed evidence views. For native ARM64/amd64 macOS benchmark preparation,
Docker VM resource admission and loopback UI routing see `docs/macbook_runbook.md`.
Never include `.env`, databases, generated datasets or image tarballs in Git.
