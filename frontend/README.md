# TraceX frontend

Investigator UI for TraceX — the SIH26146 Bitcoin UTXO intelligence platform. React + TypeScript + Vite, hand-authored mustard-neumorphic CSS (no UI framework), React Router for the 10 routes described in `docs/phase5a_handoff.md` and the project's frontend build spec.

Every page reads from the real backend in `../app` — nothing here is mocked. Where the backend doesn't yet have data (Phase 5 model metrics, an archive-case endpoint, etc.) the page says so explicitly instead of showing a fabricated number.

## Run it

Backend first, from the repo root:

```bash
docker compose up -d postgres        # or point TRACEX_DATABASE_URL at sqlite for local dev
uv run tracex-init-db
uv run uvicorn app.main:app
uv run python -m workers.runner      # separately, for import job processing
```

Then the frontend:

```bash
npm install
npm run dev
```

Opens on `http://localhost:5173`. Sign up with any name/password (min 8 characters) — the backend auto-creates the account on first signup.

## Configuration

The only runtime setting is the backend's base URL, read from `VITE_API_BASE_URL` (see `.env.example`):

```bash
cp .env.example .env.local
# edit VITE_API_BASE_URL for a hosted backend, e.g. https://tracex-api.example.com
```

Unset, it defaults to `http://localhost:8000` — correct for local dev, wrong for any hosted deployment. `.env.local` is gitignored (matches Vite's `*.local` convention already in `.gitignore`); never commit a real deployment URL as the checked-in default.

## Structure

- `src/lib/api.ts` — the single typed HTTP client; every request/response shape matches `app/api/routes.py` field-for-field. No page calls `fetch()` directly.
- `src/lib/sse.ts` — reads the case-events stream via `fetch()` + `ReadableStream`, not the native `EventSource` (which can't send the `Authorization` header this app's auth uses).
- `src/lib/auth.tsx`, `src/lib/lastCase.ts` — session token storage and the "last visited case" the nav rail falls back to from non-case-scoped pages (Dashboard, Overview).
- `src/graph/forceLayout.ts` — a small hand-rolled force-directed layout for the UTXO Graph Explorer (no `d3-force` dependency at this graph size).
- `src/pages/*` — one file per route; each documents its own data bindings and any backend gap it works around inline.

## Build

```bash
npm run build   # tsc -b && vite build
npm run lint    # oxlint
```
