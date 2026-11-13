# Failure log

- A limit-entry zone on the wrong side of the market could pass the gate. The gate now rejects it; see `tests/gate/test_rules.py`.
- Per-thousand venue symbols could prevent price consensus. The adapter normalizes symbols and prices; see `tests/runs/test_price_consensus.py`.
- A model incapable of compiling nested schemas caused failed calls before fallback. The incompatible model was removed from the configured tier; see `tests/unit/test_models_config.py`.

These examples document regression coverage, not trading performance.
