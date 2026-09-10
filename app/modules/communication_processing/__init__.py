"""Audio, social/chat, multilingual alias, and communication-link foundation (Sarthak Phase 1).

Imports transcript/diarization *results* (never runs real ASR/diarization),
parses local WhatsApp/Telegram/Instagram/generic-JSON social exports, and
builds conservative, deterministic alias/transliteration and
communication-link *candidates* for human review. See `docs/architecture/
audio-social-and-communication-processing-v1.md` and `docs/architecture/
multilingual-alias-candidates-v1.md` for the full design, and `worker.
process_job` for the entry point.

This module never accesses PostgreSQL, Neo4j, Redis, MinIO, a queue, an
HTTP client, a subprocess, or any ML/ASR/diarization model.
"""
