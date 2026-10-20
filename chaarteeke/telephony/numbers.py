"""Indian mobile-number normalisation for CLIP caller IDs. Returns None rather than guessing."""

import re

_MOBILE = re.compile(r"^[6-9]\d{9}$")


def to_e164_in(raw: str | None) -> str | None:
    """'+919876543210', '919876543210', '09876543210', '9876543210' -> '+919876543210'.

    Anything else (landline, short code, 'anonymous', empty, foreign number) -> None. The caller
    decides what to do with None; there is no default and no guess.
    """
    if not raw:
        return None
    s = re.sub(r"[\s\-()]", "", raw)
    if s.startswith("+91"):
        s = s[3:]
    elif s.startswith("0091"):
        s = s[4:]
    elif s.startswith("91") and len(s) == 12:
        s = s[2:]
    elif s.startswith("0") and len(s) == 11:
        s = s[1:]
    return f"+91{s}" if _MOBILE.match(s) else None
