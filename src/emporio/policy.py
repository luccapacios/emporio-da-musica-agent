"""The store manual, split by numbered section and looked up by topic.

The manual has ten numbered sections with clear subjects, so picking a section by topic is
the retrieval step. The model chooses the topic through the `get_policy` tool.
"""

from __future__ import annotations

import re
from pathlib import Path

# Topic name exposed to the model -> section number in the manual.
TOPICS: dict[str, int] = {
    "about": 1,  # what the store sells, company data, address, contact
    "hours": 2,
    "payment": 3,  # payment methods, PIX discount, installment rules
    "returns": 4,  # right of withdrawal, defect and preference exchanges
    "shipping": 5,
    "promotions": 6,
    "service": 7,  # service guidelines, scope, special situations, complaints
    "warranty": 8,
    "privacy": 9,
    "final": 10,
}

TOPIC_DESCRIPTIONS = {
    "about": "what the store sells (instruments only, no accessories), company data, address, phone, e-mail",
    "hours": "opening hours, Sundays and holidays",
    "payment": "payment methods, PIX discount, credit card installment rules",
    "returns": "returns, right of withdrawal (7 days), defect exchange (30 days), preference exchange, non-eligible items",
    "shipping": "delivery fees and times in Campo Grande and other cities, carriers, tracking codes",
    "promotions": "promotion calendar and rules (not cumulative, no rain check)",
    "service": "service guidelines, out of stock, discontinued, expired promotions, complaints",
    "warranty": "legal 90-day warranty, manufacturer warranty, what is not covered",
    "privacy": "personal data use and deletion (LGPD)",
    "final": "final provisions",
}

_SECTION_HEADING = re.compile(r"^## (\d{1,2})\. ", re.MULTILINE)


class Policies:
    def __init__(self, sections: dict[int, str]):
        self._sections = sections

    @classmethod
    def load(cls, path: Path) -> "Policies":
        text = path.read_text(encoding="utf-8")
        matches = list(_SECTION_HEADING.finditer(text))
        sections: dict[int, str] = {}
        for i, match in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            sections[int(match.group(1))] = text[match.start() : end].strip()
        return cls(sections)

    def get(self, topic: str) -> str:
        if topic not in TOPICS:
            raise KeyError(f"unknown topic {topic!r}; use one of {sorted(TOPICS)}")
        return self._sections[TOPICS[topic]]
