"""Input guardrail for scraped and third-party text (OBSERVABILITY_AND_GUARDRAILS.md §2.2).

Everything a model will read that came from outside — headlines, article excerpts, market
questions — passes through `scrub()`: HTML stripped, control characters removed, length capped,
and instruction-shaped text *flagged* (never silently dropped, so the Red Team can see it).
Scrubbed text is data. It never becomes a parameter (CLAUDE.md rule 6).
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

HEADLINE_LIMIT = 200
EXCERPT_LIMIT = 600

_TAGS = re.compile(r"<[^>]+>")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACE = re.compile(r"\s+")
_INJECTION = re.compile(
    r"(ignore|disregard|forget)\s+(all\s+)?(previous|prior|above|earlier)\s+"
    r"(instructions|rules|prompts?|messages?)"
    r"|\b(system|assistant|developer)\s*:"
    r"|\byou\s+(must|are\s+now|should\s+now)\s+(approve|buy|sell|ignore|obey|act)"
    r"|</?\s*(evidence|system|instructions?)\s*>"
    r"|\bnew\s+instructions\b"
    r"|\bjailbreak\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Scrubbed:
    text: str
    truncated: bool
    injection_suspect: bool

    def as_evidence(self, source: str) -> str:
        """Wrap for a prompt. The body can no longer contain a closing tag of its own."""
        body = self.text.replace("<", "‹").replace(">", "›")
        flag = ' injection_suspect="true"' if self.injection_suspect else ""
        return f'<evidence source="{source}"{flag}>{body}</evidence>'


def scrub(raw: str | None, limit: int = HEADLINE_LIMIT) -> Scrubbed:
    if not raw:
        return Scrubbed("", False, False)
    suspect = bool(_INJECTION.search(raw))
    text = html.unescape(_TAGS.sub(" ", raw))
    text = _CONTROL.sub("", text).replace("\xa0", " ")
    text = _SPACE.sub(" ", text).strip()
    suspect = suspect or bool(_INJECTION.search(text))
    truncated = len(text) > limit
    if truncated:
        text = text[: limit - 1].rstrip() + "…"
    return Scrubbed(text, truncated, suspect)
