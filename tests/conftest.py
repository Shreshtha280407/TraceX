"""Session-wide test defaults.

Pytest imports this module before collecting any test file, and app.config
builds its process-wide `settings` singleton the first time anything imports
it -- so setting this here, at module scope, guarantees the env var is in
place before that first import, however it happens to be triggered.

TRACEX_ALLOW_DEV_ACTOR_HEADER defaults to "0" in app.config.Settings.from_environment()
(a real deployment must opt in explicitly). The test suite intentionally opts
in here so the many existing tests that authenticate via `X-TraceX-Actor`
keep working without editing each one individually.
"""

import os

os.environ.setdefault("TRACEX_ALLOW_DEV_ACTOR_HEADER", "1")
