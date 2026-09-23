# Phase 8 acceptance checklist

Transcribed verbatim from the master plan's §23 "Final backend MVP
acceptance checklist" (`TraceX_Backend_MVP_Master_Implementation_Plan.pdf`,
Version 1.0, page 30). This is the live tracking document Phase 8 Parts
2-4 check off against as they run — **Part 1 does not fill in results for
anything Parts 2-4 are responsible for testing.**

A box is checked (`[x]`) only when this multi-session effort's own history
already has concrete, citable evidence the item is fully true. Every other
item stays unchecked, with a one-line status distinguishing "not yet
verified this phase" (nobody has tested it yet) from "confirmed partial/
gap" (it has been tested, and the result is a real, documented limitation
-- not the same thing as untested).

The master plan's own release rule, verbatim, governs every item below:

> Release rule: no headline capability may exist only as a mock, manually
> edited graph or unreproducible notebook. The backend MVP is accepted
> only when raw inputs reproduce the claimed outputs through committed
> code and documented commands.

## 23.1 Functional

- [ ] Supported formats: PDF, DOCX, scan, TXT, CSV, XLSX, JSON, DB-style
  export, MP4, AVI, MKV, JPG, PNG, WAV, MP3 and M4A.
  **Status**: not yet verified this phase -- Part 1 did not exercise
  format-by-format ingestion; the source pipelines exist in code
  (structured/document/media/communication processing modules) but no
  per-format acceptance pass has been run in this effort's own history.
- [ ] Every source produces canonical observations or an explicit,
  documented rejection/failure.
  **Status**: partial -- confirmed for the source types this effort
  actually exercised live (CDR, financial, chat/social, generic JSON,
  documents), including an explicit refuse-rather-than-guess path
  (`evidence_files_for_case` raises `TruthGenerationIncomplete` for an
  unmapped `source_type` rather than guessing one). Video/image/audio
  source types were not independently re-verified this phase.
- [x] Entity resolution handles exact IDs, aliases, spelling variants,
  transliteration and vector candidates without automatic uncertain
  merges.
  **Status**: true, with live proof. `exact_identifier_blocks` (Tier 1,
  ADR-029), `lexical_blocks` (Tier 2 -- aliases/spelling-variants/
  transliteration, ADR-030), and `case_tfidf_vectors`/`vector_linked_
  candidates` (Tier 3, ADR-032) all confirmed working against real
  Fulcrum/Nightfall data this session. "Never automatic merge" is
  `EntityRepository.get_or_create_entity_for_observation`'s own hard
  invariant, confirmed by
  `test_exact_identifier_match_never_merges_and_needs_no_candidate` and
  by every live `--resolve-entities` run this effort ever performed
  (entities created 1:1 from observations; only reviewable *candidates*
  are ever produced, never a merge).
- [ ] Neo4j graph contains long-lived entities, explicit temporal events
  and evidence/provenance paths.
  **Status**: not yet verified this phase -- the projection mechanism
  (`intelligence/projection.py`, `SUPPORTED_BY_OBSERVATION`) is real code
  from earlier phases, but this effort's own work (WP-2 entity resolution,
  hypothesis citation) deliberately never touches Neo4j at all (confirmed:
  zero Neo4j footprint, ADR-030/ADR-032's own scope notes) and no live
  Neo4j query against real long-lived-entity/temporal-event content was
  run in this phase.
- [ ] At least one real cross-modal incident thread and one temporal motif
  reproduce from Nightfall source files.
  **Status**: confirmed gap, not untested. Nightfall's real candidate
  graph (live-inspected this session, ADR-032) shows no engineered cross-
  identity/bridge structure at all -- the corpus itself was never authored
  with the master plan §17.2 two-community/bridge scenario. See `docs/qa/
  known-limitations.md`'s Phase 8 Part 1 entry, pinned to `TraceX-
  Synthetic-Data` commit `7a609b0096a3578cba1fafc21ee14656a8503ece`. This
  is a content-authoring gap, not a pipeline defect -- the pipeline itself
  is confirmed working on the real data it was given.
- [ ] Hypothesis contains support, contradiction, reasons, confidence
  components and review state.
  **Status**: partial, with live proof for part of it. `supporting_
  observation_ids`/`supporting_candidate_ids`/`supporting_entity_
  resolution_candidate_ids` (support) and `needs_review`/`accepted_by_
  reviewer`/`rejected_by_reviewer` (review state) are real, live-proven
  this session (ADR-031: a real hypothesis created via `POST /hypotheses`
  citing a real WP-2 candidate, reviewed to `accepted_by_reviewer`).
  `HypothesisRecord` itself carries no distinct "contradiction" or
  "confidence" field -- those live on the *cited* candidate
  (`contradiction_reasons`/`vector_score` on `EntityResolutionCandidateRecord`,
  `total_rules_score` on Phase 5's `ScoredCandidate`), not on the
  hypothesis object. Worth a deliberate decision on whether that indirect
  path satisfies this item's intent.
- [ ] Human review memory survives restart and preserves rejected
  candidates, notes and handoff context.
  **Status**: partial -- the underlying storage is real, durable,
  append-only Postgres (`candidate_review_decisions`/`entity_review_
  decisions`/`hypothesis_actions`, confirmed append-only by a live-
  triggered `DELETE` rejection this effort hit directly: "integrity
  records are append-only"), so restart-survival is architecturally real,
  not in-memory. No live "restart the stack, confirm review history is
  still there" test was run in this phase specifically.

## 23.2 Security and integrity

- [x] Admin-created accounts, authentication, RBAC and case/classification
  ABAC work through APIs.
  **Status**: true, with repeated live proof this session. Admin
  bootstrap (`create-admin` CLI), login, and the `CaseAction`/`CaseRole`
  RBAC matrix (`authorize_case_action`) were exercised live and
  repeatedly throughout this entire effort, most recently in Phase 8
  Part 1's own fresh-clone worker-credential re-verification and every
  hypothesis/entity-resolution live-proof run.
- [ ] Case isolation covers PostgreSQL, Neo4j, pgvector and MinIO and
  worker submissions.
  **Status**: partial. PostgreSQL case-scoping: confirmed live (every
  entity-resolution/hypothesis query is `case_id`-scoped; cross-case
  citation explicitly tested and rejected with `HypothesisValidationError`
  this session). pgvector: confirmed via `retrieval._single_case` and
  `PgvectorCandidateStore`'s own case-scoped queries
  (`test_pgvector_search_binds_the_descriptor_case_before_returning_
  candidates`). Worker submissions: confirmed live this phase (cross-scope
  claim correctly 403s with an audited `worker_processor_scope_denied`
  event). Neo4j and MinIO case isolation were not independently
  re-verified by this effort's own history.
- [ ] Original evidence verifies against its ingestion hash.
  **Status**: not yet verified this phase -- this is Phase 2 evidence-
  lifecycle foundation work, not something this effort's own sessions
  exercised directly.
- [ ] Append-only audit chain, Merkle root and Ed25519 signature verify
  after restart/export.
  **Status**: partial. Append-only enforcement is confirmed real and
  live (a direct `DELETE` attempt against `entity_review_decisions` this
  effort ran was rejected by a real PostgreSQL trigger). Ed25519 key
  *generation* was exercised live this phase (`integrity.cli
  generate-key`, Phase 8 Part 1 Step 2). A full checkpoint-creation +
  Merkle-root + signature-verify-after-restart/export cycle was not run.
- [ ] Tampering, deletion, reordering and unauthorized access produce
  failures and audit events.
  **Status**: partial. Unauthorized access: confirmed live, repeatedly
  (403 + audited `worker_processor_scope_denied`). Deletion: confirmed
  live (append-only trigger rejection, see above). Tampering/reordering
  of the Merkle chain specifically was not tested by this effort.

## 23.3 Reliability and measurement

- [ ] Retries do not duplicate evidence, observations, nodes,
  relationships or review actions.
  **Status**: partial. Strong, repeated live evidence for the paths this
  effort actually built/touched: `create_hypothesis`'s deterministic-ID
  replay (live-proven: an identical resubmission returns the same
  `hypothesis_id`, `is_new=False`), `get_or_create_entity_for_
  observation`'s idempotency by `(case_id, source_observation_id)`,
  `upsert_candidate`'s versioned idempotency key. No system-wide retry-
  storm test across every evidence/observation path was run.
- [ ] A failed chunk resumes without rerunning completed chunks.
  **Status**: not yet verified this phase.
- [ ] GPU loss produces an explicit degraded state while structured
  intelligence continues.
  **Status**: partial. `MEDIA_DETECTOR_DEVICE=auto`'s CPU fallback was
  exercised live this phase (media-worker ran successfully under the
  `gpu-worker` Compose profile on a GPU-less machine, Phase 8 Part 1
  Step 2) -- structured intelligence (entity resolution, evaluator runs)
  is confirmed completely independent of media/GPU availability. Whether
  an *explicit* degraded-state signal is surfaced anywhere (a health
  field, a log event) was not confirmed.
- [x] Performance and accuracy reports name dataset hash, source profile,
  model/config, hardware and commit.
  **Status**: resolved this phase, with live proof. `OfflineEvaluationReport`
  gained two new required fields, `git_commit` and `hardware`
  (`app/core/build_info.py`'s `resolve_git_commit`/`resolve_hardware_label`
  -- explicit override, then an env var
  (`TRACEX_BUILD_COMMIT`/`TRACEX_HARDWARE_LABEL`) for containerized
  deployments where `.git` isn't present (`.dockerignore` excludes it),
  then a live `git rev-parse HEAD`/`platform.node()` fallback; never
  raises, degrades to `"unknown"` rather than a missing field), populated
  automatically at both construction sites (`deferred_evaluation_report`
  and `run_offline_evaluation`) -- no caller changes needed. Purely
  additive: `dataset_hash`/`truth_hash`/`rules_config_hash`/`command`/
  `metrics`/`deferred` are all unchanged. Live-verified via the real CLI
  (`intelligence_worker.py --evaluate`, no mocking): printed report
  showed `"git_commit": "f4febf361f188ecf89488c2877550f8bd81c24cc"`
  (this checkout's real HEAD, matching the `phase8-rc1` tag) and
  `"hardware": "archlinux"` (the real hostname). 4 new targeted tests in
  `tests/unit/test_build_info.py` (override/env-var/live-fallback/
  degrades-to-unknown-never-raises, all passing) plus one dedicated
  "real report generation, both new fields populate, all pre-existing
  fields unchanged" test in `test_intelligence_evaluation.py` (27/27
  passed total across both files, no test broken by the schema change).
- [x] The frozen scorer and all thresholds are versioned and reproducible.
  **Status**: true, strongly confirmed. `scoring.py` was never touched by
  this entire effort (an explicit, repeatedly-honored constraint across
  every session). `RULES_CONFIG_HASH` was confirmed byte-identical
  (`ee833acada1f54b7a1b56d5f5bd907a2305aa84057e093420e38b8266f69dbce`)
  across every evaluator run this whole effort ever performed, and
  `_require_frozen_relationship_configuration`'s Gate-C check was
  confirmed to run before any correlation-pipeline data access.

## 23.4 Release documentation

- [ ] README setup and architecture are current.
  **Status**: not yet verified this phase -- README.md itself was not
  read/re-audited in Phase 8 Part 1 (the runbook checked was `docs/
  runbooks/local-development.md`, confirmed accurate against what Step 2
  actually required).
- [ ] All phase gates are marked complete in mvp-progress.md.
  **Status**: not yet verified this phase -- file exists (confirmed
  present on the `phase-8` branch), content not audited this phase.
- [x] Test matrix and latest results match the release commit.
  **Status**: true, freshly confirmed as part of this exact phase. Phase
  8 Part 1 Step 2 ran the full suite against a genuinely fresh clone of
  the tagged `phase8-rc1` commit (`f4febf361f188ecf89488c2877550f8bd81c24cc`)
  and reproduced the working checkout's exact known-good numbers: 2632
  passed, 12 skipped, 0 failed (after fixing one real, now-documented
  `.env.example` gap -- see Part 1's own report).
- [x] Known limitations remain visible and honest.
  **Status**: true, strongly evidenced. `docs/qa/known-limitations.md`
  has been actively, honestly maintained throughout this entire effort --
  every real gap found (the Nightfall content-authoring gap, the now-
  closed hypothesis/WP-2 citation gap, the now-closed Tier 1-3 vector-
  quality gap, the `.env.example` gap this exact phase found) was
  documented precisely, including gaps that reflect unfavorably on prior
  work, never silently smoothed over.
- [ ] Local, LAN, backup/restore and integrity-verification runbooks have
  been executed from a clean environment.
  **Status**: partial. The *local* runbook was executed from a genuinely
  clean environment this exact phase (Phase 8 Part 1 Step 2: fresh clone,
  full build/up/migrate/credential-provisioning/pytest cycle, on isolated
  ports, independently reproducing the working checkout's results). The
  LAN runbook (the eventual three-laptop rehearsal), backup/restore
  runbook, and integrity-verification runbook were not executed this
  phase.
