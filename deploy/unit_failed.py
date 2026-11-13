"""Telegram alert for a failed ARGUS unit — the target of systemd's `OnFailure=`.

    OnFailure=argus-failure@%n.service   ->   unit_failed.py argus-review.service

A manager, review or reflection run that exits non-zero, or is killed at its TimeoutStartSec,
used to leave its trace in journald and nowhere else (review 2026-10-07, J-N12, J-N3): the units
have no healthcheck of their own, because HEALTHCHECKS_URL is the watchdog's heartbeat (F-126).

Run with the project's venv interpreter from /opt/argus-project, as the `argus` user. It reads the
Telegram settings through argus.config.Settings and never prints the token. Exits 1 when the
alert could not be delivered, so `systemctl --failed` shows that the alert itself failed.
"""

from __future__ import annotations

import re
import sys

from argus.notify.telegram import Notifier

# Unit names only. Telegram parses the message as HTML, and a stray `<` drops all of it (O-1).
_SAFE = re.compile(r"[^A-Za-z0-9@._-]")


def main(argv: list[str]) -> int:
    unit = _SAFE.sub("", argv[1]) if len(argv) > 1 else ""
    unit = unit or "an ARGUS unit"
    text = (
        f"ARGUS: {unit} FAILED. Nothing retries it before its next timer tick. "
        f"On the box: journalctl -u {unit} -n 50"
    )
    sent = Notifier.from_settings().send(text)
    print(f"alert for {unit}: {'sent' if sent else 'NOT delivered (Telegram not configured?)'}")
    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
