"""Profanity filtering helpers for the Lucifer BombSquad 1.8.0 server.

This module is pure Python and does not depend on a specific Ballistica API.
"""

import re

# Keep PATTERN available for compatibility with code that imports it.
# Explicit word boundaries on common terms reduce accidental matches inside
# unrelated words while still allowing the existing transliteration variants.
PATTERN = (
    r"\bfu+c+k+\w*\b|"
    r"\bfu+c+(?:$|\b)|"
    r"\bfu+k+(?:$|\b)|"
    r"\b\w*ph+u*c+k\w*\b|"
    r"\b\w+ch+o+d+\b|"
    r"\brandi+\w*\b|"
    r"\bchu+t\w*\b|"
    r"\bchh+a+k+[ae]\b|"
    r"\bhijd\w*\b|"
    r"\blund\b|"
    r"\bass\b|"
    r"\basshole\b|"
    r"\bbi*tch\b|"
    r"\bcock\b|"
    r"\bga+nd\b|"
    r"\bga+ndu\b|"
    r"\btharki\b|"
    r"\btatti\b|"
    r"\blod\w*\b|"
    r"\bjha+nt\w*\b|"
    r"\bpu+s+y\b|"
    r"\bpu+z+y\b|"
    r"\bdi+c+k\b|"
    r"\b(?:[mb]+c+)+\b|"
    r"\b[mb]+[^a-zA-Z]?c+\b|"
    r"\bf.u.c.k\b|"
    r"\bb\w*s\w?d\w?k\b|"
    r"\bm.{0,4}d.?a.{0,8}c.?h.?o.?d\b|"
    r"\bb.+n.?c.?h.?o.?d\b|"
    r"\bcunt\b|"
    r"\bmy+r+e\b|"
    r"\bth+y+r\b|"
    r"\bth+y+i+r\b|"
    r"\bth+aa+y+o+l+i\b|"
    r"\bth+a+y+o+l+i\b|"
    r"\bku+nn+a+n\b|"
    r"\bna+y+i+n+t+e\b|"
    r"\bpu+ll+u\b|"
    r"\bla+(?:u|v)+d+\w\b|"
    r"\bchu+d\w*\b|"
    r"\bsex+\b|"
    r"\bbo+b(?:s|z)\b|"
    r"\bpo+r+n\b|"
    r"\bni+p+le+\b"
)

# Compile once instead of compiling the expression for every chat message.
_PROFANITY_RE = re.compile(PATTERN, re.IGNORECASE)


def censor(message: str) -> str:
    """Replace matched profanity with asterisks, preserving other text.

    Non-string inputs are converted to strings defensively. Normal chat input
    should already be a string; this guard prevents an unexpected caller from
    raising a TypeError in the chat pipeline.
    """
    if not isinstance(message, str):
        message = str(message)
    if not message:
        return message
    return _PROFANITY_RE.sub(
        lambda match: "*" * len(match.group(0)), message
    )
