"""TraceX access control: authentication, sessions, and case-scoped RBAC/ABAC.

See `docs/architecture/access-control-v1.md` for the design and
`docs/decisions/ADR-003-authentication-and-case-scoped-access-control.md`
for the reasoning behind the token, session, and policy decisions below.
"""

from __future__ import annotations
