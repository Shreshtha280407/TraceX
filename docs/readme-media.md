# TraceX README media and recorded walkthrough

The [master README](../README.md) combines original motion artwork with real recordings of the investigator console. The two types have different provenance.

## Real UI recordings

The Playwright package records the shipped React UI against a real local FastAPI process and a separate worker. The runner initializes a fresh temporary SQLite database, evidence directory, random session secret and synthetic source. It does not connect to the owner's database or evidence vault.

| Clip | Recorded duration | What happens |
|---|---:|---|
| [Evidence intake](assets/readme/intake.webm) | 22.48 s | Browser signup, synthetic case creation, pinned v2 selection, upload, real worker progression and dashboard |
| [Graph exploration](assets/readme/graph.webm) | 14.64 s | Stored peeling candidate selection, actual UTXO path, zoom and source-centred flow query |
| [Review and export](assets/readme/review.webm) | 19.84 s | Capacity controls, group proposition, original-record replay, persisted independent group triage and JSON download |

The source contains 1,000 canonical transactions generated with the fixed seed `tracex-readme-cinematic-20261005` using the existing bounded synthetic generator. Evaluation truth is never uploaded to the product.

Before the recorded triage, the case has **184 underlying findings, 140 investigation groups, 100 queued groups and 40 additional unresolved groups**. These are measured fixture counts. They do not establish large-scale compression or precision.

The successful capture runs **21 checks**, including required core-stage outcomes, original-source SHA-256 agreement, complete grouping coverage, real record replay, persisted review, unchanged member finding dispositions and a successful UI download. No browser JavaScript exceptions or external browser requests were observed. The runner intercepts requests only to reject external origins; it never fulfills fabricated product responses.

The disposable stack intentionally has **no GeoIP cache**. Missing enrichment remains visible and the overall analysis reports degraded coverage. This capture is not PostgreSQL acceptance, whole-host isolation evidence, a quality evaluation, or a scale benchmark.

The [capture manifest](assets/readme/capture-manifest.json) retains the exact recorded timestamp, source hash, Git base, stage outcomes, counts, checks and media hashes. Captures are performed against the working tree; the base commit identifies repository history, not a new committed media release.

## GIFs and stills

The full videos are continuous Playwright browser recordings at **1600 × 1000**. README GIFs are FFmpeg conversions at 7 fps, 880 pixels wide, with a bounded palette for GitHub loading. They retain the video timeline. No screen content is drawn over, replaced or fabricated.

The seven PNG screenshots come from the same live run. The README exposes them as static alternatives for readers who prefer stills. Native GitHub Markdown supports embedded GIFs and image links; the WebM links open the full recording where the browser/viewer supports it.

## Original identity and diagrams

The animated cover is **illustrative artwork**, clearly labelled in the image. Its transaction nodes are a visual metaphor, not extracted case evidence.

- [Cover source](assets/readme/hero.html): code-native canvas/CSS artwork using the product's self-hosted fonts.
- [Art renderer](../scripts/render_readme_art.mjs): Playwright renders deterministic, lossless artwork frames; FFmpeg produces the looping GIF and a static PNG poster.
- [Architecture diagram](assets/readme/architecture.svg): browser, API, worker and shared control/evidence storage.
- [Evidence contract](assets/readme/evidence-contract.svg): preserved bytes, versioned claims and independent review.

The artwork and diagrams use TraceX's amber, charcoal, muted blue and sage palette. The diagrams are native SVG; they remain readable in GitHub's image renderer without scripts or external assets.

## Regenerate

Prerequisites: the repository's Python and frontend dependencies, **Google Chrome**, **FFmpeg**, and a writable `/tmp`. The scripts use Playwright's `channel: "chrome"`. Installation of dependencies is a preparation step; the recording itself uses local requests.

From the repository root:

```bash
uv sync --frozen --extra dev --extra ml
npm --prefix frontend ci
uv run python scripts/capture_readme.py
node scripts/render_readme_art.mjs
```

`capture_readme.py` builds the UI with an empty `VITE_API_BASE_URL` so the built frontend uses the same origin as the temporary API. It allocates a local port, starts its own API/worker, records three scenes and stops those processes in a `finally` block. Existing database settings, candidate artifacts, dev identity bypass and GeoIP settings are overridden for the isolated run. Temporary synthetic files/logs remain in the printed `/tmp/tracex-readme-*` directory for inspection.

`--skip-build` is available only when `frontend/dist` was already built with the same-origin setting. `--output PATH` writes media to another directory for review; the README expects the default `docs/assets/readme` location. Regeneration overwrites those generated media files.

The art renderer writes deterministic frame images under `/tmp/tracex-readme-art`, then updates `hero.gif` and `hero.png`. Its output is separate from the product-recording manifest.

## Present the investigation

1. **Open the case dashboard.** Explain accepted transactions, underlying observations, queued groups and the separate backlog.
2. **Open the graph.** Follow the accepted UTXO spend path and distinguish observed spends from heuristic associations.
3. **Open an evidence package.** Reopen one original record and inspect its exact locator/hash.
4. **Review a group proposition.** Record a reason and show that member finding decisions remain independent.
5. **Open network intelligence.** Identify which context was supplied, enriched or unavailable.
6. **Generate the export.** Download the evidence/history bundle and show its contents.

Use a fresh synthetic case for each rehearsal. Keep scale, quality and isolation claims tied to their own implementation reports.
