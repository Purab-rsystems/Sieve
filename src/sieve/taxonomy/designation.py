"""Designation mapping and authority (PRD module M6).

Takes a free-text job title and answers four questions:

    canonical title     'VP Tech & Architecture'  ->  'VP of Engineering'
    seniority           C-Suite | VP | Director | Manager | Individual Contributor
    authority           does this person hold budget or signing power
    committee role      Economic Buyer | Champion | Influencer | None

Matching runs three passes, cheapest first, exactly as the header resolver
does. An exact alias hit costs a dict lookup. A regex pass handles the long
tail. A fuzzy pass catches spellings nobody anticipated.

Disqualifying tokens are checked before any of that. 'Data Engineer Intern'
must never resolve to 'Data Engineer', and 'Former CTO' must never resolve to
an authoritative C-Suite role, however well the rest of the string matches.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from rapidfuzz import fuzz, process

from ..normalize.values import clean_str

__all__ = ["Designation", "TitleTaxonomy", "load_taxonomy", "DEFAULT_TAXONOMY_PATH"]

DEFAULT_TAXONOMY_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "titles.yaml"
)

#: Below this similarity the fuzzy pass declines rather than guessing.
FUZZY_FLOOR = 88.0


@dataclass(frozen=True)
class Designation:
    """What M6 concluded about one title."""

    title_raw: str | None
    canonical: str | None
    seniority: str
    seniority_rank: int
    is_authoritative: bool
    committee_role: str | None
    is_ambiguous: bool
    match_basis: str        # 'alias' | 'pattern' | 'fuzzy' | 'disqualified' | 'unmatched'
    confidence: float

    @property
    def matched(self) -> bool:
        return self.canonical is not None


class TitleTaxonomy:
    """The loaded taxonomy, ready to resolve titles."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.version = config.get("version", "0")
        self.seniority_levels: dict[str, int] = config["seniority_levels"]
        self._disqualifying = [t.lower() for t in config.get("disqualifying_tokens", [])]
        self._ambiguous = [t.lower() for t in config.get("ambiguous_tokens", [])]
        self._roles: list[dict[str, Any]] = config["roles"]

        # Pass 1: exact alias -> role. Built once at load.
        self._by_alias: dict[str, dict[str, Any]] = {}
        for role in self._roles:
            for alias in role.get("aliases", []):
                self._by_alias.setdefault(self._key(alias), role)

        # Pass 2: compiled patterns, kept in declaration order.
        self._patterns: list[tuple[re.Pattern[str], dict[str, Any]]] = [
            (re.compile(pattern, re.I), role)
            for role in self._roles
            for pattern in role.get("patterns", [])
        ]

        # Pass 3: the alias list, for fuzzy comparison.
        self._alias_keys = list(self._by_alias)

    @staticmethod
    def _key(text: str) -> str:
        """Normalize a title for exact comparison."""
        text = str(text or "").lower().strip()
        text = re.sub(r"[^\w\s&]", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def resolve(self, title: str | None) -> Designation:
        """Map one title onto the taxonomy."""
        cleaned = clean_str(title)
        if cleaned is None:
            return self._unmatched(None, "unmatched")

        lowered = cleaned.lower()

        # Disqualifying tokens win over everything else.
        if any(token in lowered for token in self._disqualifying):
            return self._unmatched(cleaned, "disqualified")

        role, basis, confidence = self._find(cleaned, lowered)
        if role is None:
            return self._unmatched(cleaned, "unmatched")

        seniority = role["seniority"]
        return Designation(
            title_raw=cleaned,
            canonical=role["canonical"],
            seniority=seniority,
            seniority_rank=self.seniority_levels.get(seniority, 0),
            is_authoritative=bool(role.get("authority", False)),
            committee_role=role.get("committee"),
            is_ambiguous=self._is_ambiguous(lowered, seniority),
            match_basis=basis,
            confidence=confidence,
        )

    def _find(
        self, cleaned: str, lowered: str
    ) -> tuple[dict[str, Any] | None, str, float]:
        key = self._key(cleaned)

        role = self._by_alias.get(key)
        if role is not None:
            return role, "alias", 1.0

        for pattern, role in self._patterns:
            if pattern.search(lowered):
                return role, "pattern", 0.9

        match = process.extractOne(
            key, self._alias_keys, scorer=fuzz.token_set_ratio,
            score_cutoff=FUZZY_FLOOR,
        )
        if match is not None:
            return self._by_alias[match[0]], "fuzzy", round(match[1] / 100.0, 3)

        return None, "unmatched", 0.0

    def _is_ambiguous(self, lowered: str, seniority: str) -> bool:
        """PRD M4 SENIORITY_AMBIGUITY: a title with no distinct executive rank."""
        if seniority in {"C-Suite", "VP", "Director"}:
            return False
        return any(token in lowered for token in self._ambiguous)

    def _unmatched(self, title: str | None, basis: str) -> Designation:
        lowered = (title or "").lower()
        return Designation(
            title_raw=title,
            canonical=None,
            seniority="Other",
            seniority_rank=self.seniority_levels.get("Other", 0),
            is_authoritative=False,
            committee_role=None,
            is_ambiguous=(
                basis == "disqualified"
                or any(token in lowered for token in self._ambiguous)
            ),
            match_basis=basis,
            confidence=0.0,
        )


@lru_cache(maxsize=8)
def load_taxonomy(path: str | Path = DEFAULT_TAXONOMY_PATH) -> TitleTaxonomy:
    """Load and cache a taxonomy file."""
    config = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return TitleTaxonomy(config)
