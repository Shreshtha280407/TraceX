"""Phase 7 Part 1: evaluation foundation, dataset manifest, and local-model governance.

The typed referee and scoreboard later Phase 7 parts (Jasraj/Gaurav/Sarthak
download and benchmark candidates; Aditya packages locked bundles;
Shreshtha freezes the correlation approach) build against. See
`docs/architecture/phase-7-evaluation-and-model-governance.md` for the full
design and `docs/decisions/ADR-016-phase-7-evaluation-and-model-selection.md`
for the frozen evaluation rules' reasoning.

This module contains no downloaded dataset, no model weight, no benchmark
result, and no model/algorithm selection -- only the contracts and
validation those later parts are measured against.
"""

from __future__ import annotations
