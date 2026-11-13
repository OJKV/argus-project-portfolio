"""ARGUS — LLM-assisted, evidence-graded swing-trading agent for crypto perpetuals."""

__version__ = "0.1.0"

import time as _time

# When this interpreter first imported ARGUS. A run that queued behind a deploy compares it with
# the deploy history to tell whether what it already imported is the old code (argus.runs.lock).
STARTED_AT = _time.time()
