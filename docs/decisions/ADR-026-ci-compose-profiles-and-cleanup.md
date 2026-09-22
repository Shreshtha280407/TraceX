# ADR-026: CI live-infra/secret-scan/nightly, worker compose profile split, tracked-artifact cleanup

## Status

Accepted (Phase 7 Closure — WP-8).

## Context

Four smaller, mostly-non-Python gaps: G14 (CI never ran integration
tests against live infra, no secret scanning, no scheduled run), G15
(only `media-worker`/`graph-projector` had a Compose service — under one
coarse `workers` profile mixing GPU and CPU-only workers — and
`structured_processing`/`communication_processing` workers had no
Compose service at all), G18 (no backup/restore or integrity-verification
runbook existed), G19 (a stray, accidentally-committed `:memory:.ses`
file at the repository root).

## Decisions

### CI gets live service containers, matched to `.env.example`'s existing placeholders

`postgres`/`neo4j`/`redis` are added as GitHub Actions `services:`
containers with credentials identical to `.env.example`'s placeholders —
the pre-existing `cp .env.example .env` CI step is what integration test
`conftest.py` files already read via `dotenv_values()`, so **zero test
code changes** were needed for the whole integration suite to start
actually running in CI instead of self-skipping on every single run.
MinIO could not use the `services:` block (GitHub Actions has no way to
pass a service container's `command:` argument, and `minio/minio`'s
image has no working default command) — started instead via a plain
`docker run -d ... minio/minio:latest server /data ...` step, polled with
the same `mc ready local` check `compose.yaml`'s own healthcheck uses,
before any test runs.

### Secret scanning: the `gitleaks` CLI directly, not the Marketplace Action

`gitleaks/gitleaks-action` gates some functionality behind an
organization license; the underlying `gitleaks` CLI itself is
unrestricted open source. Installed from a pinned GitHub release inside
a plain `run:` step instead, avoiding any licensing ambiguity for a
private repository.

Running gitleaks against this repository's actual history (before
wiring it into CI) found two real matches — both false positives: this
codebase's `idempotency_key` parameter/variable name superficially
matches the default ruleset's `generic-api-key` regex. `.gitleaks.toml`
adds one allowlist entry scoped to that exact shape (`regexTarget =
"line"`, matching the word `idempotency_key` case-insensitively, with
optional separator) — extending gitleaks' default ruleset, never
replacing or weakening it. Verified locally: zero leaks with the config
applied, two real (false-positive) leaks without it.

### Nightly is a `schedule:` trigger on the same job, not a separate workflow file

A nightly cron (`17 3 * * *` — an off-peak, non-round-number time) re-runs
the exact same `verify` job (same live infra, same checks) that push/PR
already trigger. This catches environmental drift (a base image tag
regressing something) even on days with no open PR, without duplicating
the job definition into a second file that could silently drift out of
sync with the first.

### Compose workers split into `cpu-worker`/`gpu-worker`, not left as one `workers` profile

The prior single `workers` profile mixed `graph-projector` (no GPU
needed) with `media-worker` (GPU-capable, needs a bootstrapped ONNX
model) — an operator without a GPU had no way to start only what their
hardware supports. Split cleanly: `cpu-worker` (`graph-projector`,
`intelligence-worker`, `structured-worker`, `communication-worker`) and
`gpu-worker` (`media-worker`, `media-model-bootstrap`). A bare `docker
compose up` (no `--profile` flag) is verified unchanged — still API +
infra only (`docker compose config --services` confirmed identical
output before and after this change).

`structured-worker`/`communication-worker` are new Compose services
(previously missing entirely) running `--once`, not `--loop` — neither
worker module has a real poll-loop mode in this phase (each module's own
CLI help text already states "No daemon or polling mode exists in this
phase," a pre-existing, deliberate Phase 7 Part 2/4 scope boundary this
WP did not touch). `restart: unless-stopped` on a `--once` command is
documented honestly in the runbook as an imperfect substitute for a real
poll loop (each restart is one more claim attempt with Docker's own
restart backoff between attempts), not silently presented as equivalent
to `graph-projector`/`media-worker`'s real `--loop` modes.

### Backup/restore and integrity-verification runbooks document existing capability, add no new code

Both runbooks are pure documentation. `integrity-verification.md` walks
through every existing `integrity/cli.py` command (`generate-key`,
`rotate-key`, `list-keys`, `build-checkpoint`, `checkpoint-once`/`-loop`,
`verify`, `export`, `archive`) end to end. `backup-restore.md` documents
standard `pg_dump`/`neo4j-admin database dump`/`mc mirror` procedures
against `compose.yaml`'s existing services and volumes, and explicitly
ties restore verification back to the integrity-checkpoint chain: a
checkpoint sealed before a restore point should still verify; one sealed
after it legitimately won't (real data loss from the restore, not
tampering) — the runbook teaches an operator to distinguish the two
rather than treating every post-restore verification failure as an
incident.

### `:memory:.ses` deleted, root cause partially confirmed

Traced to commit `6af8ac6` ("Completed gaurav/phase-4 (#35)", 2026-09-14)
via `git log --all --diff-filter=A`. Confirmed **not** produced by this
repository's core media dependencies (`opencv-python-headless`,
`onnxruntime`) via a clean-environment import test. The optional heavy ML
extras from that same commit (`paddleocr`/`paddlepaddle` under the `ocr`
group, `ultralytics`/`torch`/`pyannote` under the `gpu`/`av` groups) were
not installed in this environment to test further — reproducing the
exact write would require installing one of those extras and tracing
file creation during actual model instantiation, which this WP's time
budget did not extend to. The file is deleted from the working tree and
`.gitignore`d going forward regardless of which library writes it; the
investigation is documented as partial, not fabricated as conclusive.

## Deferred / explicitly out of scope

- Confirming gitleaks CI-wired against a live GitHub Actions runner (only
  verified locally in this environment — the workflow YAML itself parses
  correctly and the `gitleaks` command was verified to behave correctly
  against this exact repository; the GitHub Actions execution itself is
  implemented, not run-and-observed, since that requires a real push/PR).
- Definitively identifying which library writes `:memory:.ses`.
- A CI job for the Gate B/benchmark suites (deliberately excluded from
  every push/PR and the nightly run both — those suites need real model
  weights and datasets neither CI environment has; they remain a
  developer-machine-only, manually-invoked capability, unchanged by this
  WP).

## Consequences

- Every PR now genuinely exercises the integration test suite, not just
  the unit suite -- a meaningfully stronger CI gate.
- A leaked secret/credential is caught before merge, not after.
- `docker compose --profile cpu-worker up -d` now covers every non-GPU
  worker this codebase has, including two that previously had no way to
  run under Compose at all.
