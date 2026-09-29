"""Dependency-free constants shared by the FastAPI routes and the anomaly stack.

Deliberately kept outside the `app.ml` package: importing any `app.ml.*` submodule
runs `app/ml/__init__.py`, which imports `app.ml.facts` and `app.ml.stack` and, with
them, numpy/scikit-learn. A constant module living inside that package would drag
those imports into the API process regardless of its own content, defeating the
reason this module exists. Living at `app/` instead avoids that entirely.
"""

from __future__ import annotations

#: Must stay identical to the finding rows the anomaly stack writes via
#: `app.ml.findings`; imported from here by both that module and `app.api.routes`
#: so the two can never drift apart.
ML_RULE_VERSION = "anomaly-stack-v1"
