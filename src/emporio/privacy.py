"""Masking of card numbers before a message reaches the model or the database.

Used by every interface (web API and terminal chat), so a pasted card number is never sent
to the model provider or stored in the conversation history.
"""

from __future__ import annotations

import re

CARD_MASK = "[número de cartão removido]"

# A run of digit groups split by short runs of spaces (any kind, as pasted from banking apps
# and PDFs), dots or hyphens, not part of a "+55 ..." phone.
_DIGIT_RUN = re.compile(r"(?<![\d+])\d+(?:[\s.-]{1,3}\d+)*")
CARD_MAX_DIGITS = 19


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def mask_cards(text: str) -> str:
    """Replace card numbers (13 to 19 digits, Luhn valid, spaces, dots or hyphens allowed)."""
    def fix(match: re.Match) -> str:
        run = match.group(0)
        groups = list(re.finditer(r"\d+", run))
        pieces, pos, i = [], 0, 0
        while i < len(groups):
            # The longest span of whole groups starting here that is a valid card number. Spans
            # only grow, so the walk stops past 19 digits and a long run stays linear.
            span, digits = None, ""
            for j in range(i, len(groups)):
                digits += groups[j].group(0)
                if len(digits) > CARD_MAX_DIGITS:
                    break
                if len(digits) >= 13 and _luhn(digits):
                    span = j
            if span is None:
                i += 1
                continue
            pieces += [run[pos:groups[i].start()], CARD_MASK]
            pos, i = groups[span].end(), span + 1
        return "".join(pieces) + run[pos:]

    return _DIGIT_RUN.sub(fix, text)
