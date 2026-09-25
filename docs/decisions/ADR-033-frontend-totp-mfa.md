# ADR-033: TOTP MFA and admin-forced password change, added for the investigator frontend

Status: **accepted**.

## Context

Phase 8 Part 2's frontend build prompt (Section 6, "Auth, RBAC & ABAC
requirements") specifies a full admin-issued-credential ceremony:
admin-provisioned account → forced password change on first login → TOTP
enrollment → two-factor login on every subsequent session → admin-only
lost-device recovery. Before writing any frontend auth code, that prompt's
own instructions required reading the real backend module first and
reporting explicitly whether TOTP already existed server-side — it did not.

`app/modules/access_control/` was, until this ADR, single-factor
email+password only, **by explicit prior decision**, documented in three
places: `docs/architecture/security-boundaries-v1.md` ("No MFA, no SSO, no
external identity provider"), `docs/qa/known-limitations.md` ("single-factor
email+password only, by explicit design for this phase"), and `README.md`'s
non-goal list. There was also no temporary-password/forced-first-login-change
mechanism at all — `AdminProvisionUserRequest.password` was, and remains, an
admin-chosen value stored directly, not a system-generated one-time secret.

Building this into the frontend without a real backend counterpart would
mean either fabricating a TOTP screen that calls nothing real (explicitly
forbidden by the same prompt's Section 9, "no mocked or stubbed content in
the delivered app") or silently reversing a documented cross-team decision
inside a shared module three other contributors' branches build on
(`dependencies.py`'s own docstring: "the integration points Nipun's later
case/evidence endpoints and Shreshtha's graph endpoints depend on"). Per
this repository's `CLAUDE.md` module-boundary rule, that call was escalated
to the operator rather than made unilaterally; the operator chose to have
this session implement real TOTP support server-side.

## Decision

1. **TOTP is hand-rolled (RFC 6238), not a new dependency.** `password.py`
   already documents this module's deliberately narrow, reviewed dependency
   list (only PyJWT and `pwdlib[argon2]` were ever approved for it). RFC 6238
   over HMAC-SHA1 is small enough (~30 lines, stdlib `hmac`/`hashlib`/`base64`/
   `struct`/`secrets` only) that reimplementing it correctly is cheaper than
   auditing a new supply-chain dependency for it, and the parameters (SHA1,
   6 digits, 30s step) match what every real authenticator app already
   expects — this is not a custom scheme, just not imported from PyPI.

2. **Additive, not breaking.** `UserRecord`/`PublicUser` gain
   `must_change_password`/`totp_secret` (never returned)/`totp_enabled`, all
   defaulted so every existing call site (the CLI bootstrap admin,
   `tests/fixtures/access_control/factories.py`) keeps compiling unchanged.
   `TokenPairResponse` gains `mfa_required`/`mfa_token`, with
   `access_token`/`refresh_token`/`expires_in` now optional — the same
   response model serves both "here is a session" (unchanged shape, the only
   shape this endpoint ever returned before) and "password was right, now
   prove the second factor" (new). `AdminProvisionUserRequest` is
   **unchanged** — the admin still supplies the initial password directly,
   deliberately, to avoid an invasive contract change across the ~12
   existing call sites (tests included) that already construct it. The
   observable behavior the frontend actually needs — a forced change before
   the account is usable — comes entirely from the new
   `must_change_password` flag on `provision_user`, not from where the
   initial password's entropy came from.

3. **The forced-change/enrollment ordering is a UI ceremony, not a
   server-wide hard gate.** `must_change_password=True`/`totp_enabled=False`
   are exposed on `PublicUser`/`MeResponse` for the frontend to act on
   (redirect to a forced-change screen, then an enrollment screen, before
   showing anything else) but no *other* endpoint in this codebase checks
   either flag. Enforcing that at the API layer would mean touching every
   authenticated route in every module for a UX sequencing concern the
   frontend can already enforce completely on its own. This is a deliberate,
   documented scope line, not an oversight.

4. **Login is two calls when TOTP is enabled, one when it isn't.**
   `POST /auth/login` returns real tokens directly exactly as before when
   `totp_enabled=False` (nothing changes for an account that hasn't
   enrolled). Once enrolled, the same endpoint instead returns a short-lived
   `mfa_token` (a distinct JWT `typ`, `mfa_pending`, carrying no session) and
   the frontend must present a live code to
   `POST /auth/mfa/login-verify` to actually obtain a session. This keeps
   the endpoint count minimal (no separate "check if MFA is needed" probe)
   at the cost of a response shape with two disjoint valid states — judged
   the better trade-off than a new endpoint or a breaking response rename.

5. **Admin-only reset clears MFA entirely, never partially trusts the old
   secret.** `POST /api/v1/admin/users/{user_id}/reset-credentials` reissues
   a temporary password, wipes `totp_secret`/`totp_enabled`, forces
   `must_change_password=True` again, and revokes every live session for
   that account. Full re-onboarding, not a narrower "just the password"
   reset — a lost/compromised device is exactly the scenario where trusting
   a pre-existing shared secret is the wrong call.

## Consequences

- `docs/architecture/security-boundaries-v1.md` and
  `docs/qa/known-limitations.md`'s "No MFA" lines are now stale and are
  updated alongside this ADR to describe what actually exists: TOTP MFA
  (not SSO, not an external identity provider — that boundary still holds).
- A new additive migration (`b2c3d4e5f6a7`) adds three nullable-or-defaulted
  columns to `users`; no backfill needed, no existing row's behavior changes
  (`must_change_password` defaults `false` at the database level — only
  newly-provisioned or admin-reset accounts ever get `true`, set by
  application code, never a schema default).
- The frontend's Phase 0 scaffold's Login/MFA page (built before this ADR,
  UI-only) is rebuilt in Phase 1 to call these real endpoints instead of
  toggling local component state.
