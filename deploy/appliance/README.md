# TraceX offline appliance (Linux)

A complete, offline TraceX installation: PostgreSQL, the API (which also serves
the web UI), and the ingestion/analysis worker, with the ML stack and an
open-source Geo-IP database (DB-IP IP-to-Country Lite, CC BY 4.0; IPtoASN, PDDL)
baked into the image. Nothing contacts the internet at runtime: the worker and
the database sit on a Docker network with no external route.

## Requirements

* 64-bit Linux with Docker Engine 24+ and the compose plugin
* 4 CPU / 8 GB RAM recommended (TraceX sizes itself to the RAM it finds:
  imports that do not fit run in bounded-memory mode automatically)
* Disk: ~3 GB for images plus ~3x the size of the data you import

## Install from the offline bundle (air-gapped host)

```sh
tar xzf tracex-offline-<version>.tar.gz
cd tracex-offline
./install.sh            # loads images.tar, writes .env with fresh secrets, starts the stack
```

Open `http://127.0.0.1:8000/` (change `TRACEX_BIND` / `TRACEX_PORT` in `.env` to
serve on the LAN), create an account, create a case and upload a CSV / JSON /
NDJSON / XML file.

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
