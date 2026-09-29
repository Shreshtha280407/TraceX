"""TraceX multi-grain anomaly stack.

Six layers over four grains, fused into one ranked review queue:

    A  structure   transaction         equal-output shape, peel geometry, batch
    B  latency     outpoint  -> tx     rapid-spend behaviour, as survival
    C  history     entity x window     surge, recurrence, reactivation, baseline
    D  population  motif count series  sudden surge in CoinJoin and peeling
    E  graph       transaction         bounded k<=2 neighbourhood + PS network context
    F  fusion      transaction         ECDF -> Stouffer, conformal budget

Run it with `scripts/run_anomaly_stack.py`, which also runs every layer alone and
every combination against the deterministic rule baseline so the best combination
is chosen from measurements rather than asserted.

Nothing here reads `evaluation_truth.json` except `app.ml.evaluate`, which uses
it only to score results; no label ever enters a feature table or a `fit()`.
"""

from app.ml.facts import Facts, load_facts
from app.ml.stack import StackConfig, StackResult, explain, run_stack

__all__ = ["Facts", "StackConfig", "StackResult", "explain", "load_facts", "run_stack"]
