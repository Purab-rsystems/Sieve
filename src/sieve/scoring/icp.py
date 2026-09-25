"""Deterministic ICP scoring (PRD module M3, Gate 1).

Four pillars, each scored 0-100 from the rubric YAML, each weighted 25%,
summed into a single 0-100 score that gates an account into Tier A, B or C.

Nothing here calls a model. The PRD's zero-token requirement means scoring is
a pure function of the row and the rubric file, which is also what makes the
bitwise reproducibility requirement achievable: run it twice on the same input
and you get the same integer.

Every score carries a breakdown naming which band fired in each pillar and on
what evidence, so a campaign manager can ask why an account scored 72 and read
the answer out of the output file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import yaml

from ..normalize.values import clean_str, to_float, to_int

__all__ = [
    "PillarResult", "IcpScore", "IcpRubric", "load_rubric",
    "DEFAULT_RUBRIC_PATH",
]

DEFAULT_RUBRIC_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "icp_adtech_modernization.yaml"
)


@dataclass(frozen=True)
class PillarResult:
    """One pillar's contribution to the total."""

    pillar: str
    label: str
    points: int             # 0-100 before weighting
    weight: float
    contribution: float     # points * weight
    band: str               # which band fired
    evidence: str | None    # the text or number that triggered it

    def as_dict(self) -> dict[str, Any]:
        return {
            "pillar": self.pillar,
            "points": self.points,
            "weight": self.weight,
            "contribution": round(self.contribution, 2),
            "band": self.band,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class IcpScore:
    """The full result for one account."""

    score: int              # 0-100, rounded
    tier: str               # 'Tier A' | 'Tier B' | 'Tier C'
    disposition: str
    pillars: list[PillarResult] = field(default_factory=list)

    def breakdown(self) -> list[dict[str, Any]]:
        return [p.as_dict() for p in self.pillars]

    def reasons(self) -> str:
        """One readable line naming each pillar's band."""
        return "; ".join(f"{p.pillar}={p.points}({p.band})" for p in self.pillars)


class IcpRubric:
    """A loaded rubric, ready to score accounts."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.campaign = config["campaign"]
        self.version = config.get("version", "0")
        self.weights: dict[str, float] = config["weights"]
        self._tiers = sorted(
            config["tiers"], key=lambda t: t["min_score"], reverse=True
        )
        self._pillars: dict[str, Any] = config["pillars"]
        self._compiled = self._compile(self._pillars)

    @staticmethod
    def _compile(pillars: dict[str, Any]) -> dict[str, Any]:
        """Pre-compile every regex once, at load."""
        compiled: dict[str, Any] = {}
        for name, spec in pillars.items():
            entry: dict[str, Any] = {"label": spec.get("label", name)}
            for key in ("bands", "text_bands"):
                if key in spec:
                    entry[key] = [
                        {
                            "points": band["points"],
                            "label": band.get("label", ""),
                            "regexes": [
                                re.compile(p, re.I) for p in band.get("patterns", [])
                            ],
                        }
                        for band in spec[key]
                    ]
            if "numeric_bands" in spec:
                entry["numeric_bands"] = spec["numeric_bands"]
            compiled[name] = entry
        return compiled

    # -- the four pillars ---------------------------------------------------

    def score(
        self,
        *,
        industry: str | None = None,
        sub_industry: str | None = None,
        category: str | None = None,
        legacy_signal: str | None = None,
        platform_description: str | None = None,
        scale_signal: str | None = None,
        modernization_signal: str | None = None,
        employees: Any = None,
        revenue_usd: Any = None,
    ) -> IcpScore:
        """Score one account. Every argument is optional and missing data
        falls through to the rubric's catch-all band rather than raising."""
        results = [
            self._score_text(
                "industry_fit",
                [industry, sub_industry, category],
            ),
            self._score_text(
                "legacy_stack",
                [legacy_signal, platform_description],
            ),
            self._score_scale(
                employees, revenue_usd, [scale_signal, platform_description]
            ),
            self._score_text(
                "modernization_triggers",
                [modernization_signal, legacy_signal, platform_description],
            ),
        ]
        total = sum(r.contribution for r in results)
        score = int(round(total))
        tier, disposition = self._classify(score)
        return IcpScore(score=score, tier=tier, disposition=disposition,
                        pillars=results)

    def _score_text(self, pillar: str, sources: Iterable[str | None]) -> PillarResult:
        """Walk the bands in order and take the first that matches."""
        spec = self._compiled[pillar]
        haystack = " | ".join(s for s in (clean_str(v) for v in sources) if s)
        weight = self.weights[pillar]

        for band in spec["bands"]:
            for regex in band["regexes"]:
                match = regex.search(haystack)
                if match:
                    evidence = self._evidence(haystack, match)
                    return PillarResult(
                        pillar=pillar, label=spec["label"], points=band["points"],
                        weight=weight, contribution=band["points"] * weight,
                        band=band["label"] or f"{band['points']}pts",
                        evidence=evidence,
                    )

        return PillarResult(
            pillar=pillar, label=spec["label"], points=0, weight=weight,
            contribution=0.0, band="no band matched", evidence=None,
        )

    def _score_scale(
        self, employees: Any, revenue_usd: Any, text_sources: Iterable[str | None]
    ) -> PillarResult:
        """Pillar 3 prefers a headcount number and falls back to signal text."""
        spec = self._compiled["platform_scale"]
        weight = self.weights["platform_scale"]
        headcount = to_int(employees)

        if headcount is not None:
            for band in spec["numeric_bands"]:
                if headcount >= band["min_employees"]:
                    return PillarResult(
                        pillar="platform_scale", label=spec["label"],
                        points=band["points"], weight=weight,
                        contribution=band["points"] * weight,
                        band=band.get("label", ""),
                        evidence=f"{headcount} employees",
                    )

        revenue = to_float(revenue_usd)
        haystack = " | ".join(s for s in (clean_str(v) for v in text_sources) if s)
        for band in spec["text_bands"]:
            for regex in band["regexes"]:
                match = regex.search(haystack)
                if match:
                    return PillarResult(
                        pillar="platform_scale", label=spec["label"],
                        points=band["points"], weight=weight,
                        contribution=band["points"] * weight,
                        band=f"{band['points']}pts from scale text",
                        evidence=self._evidence(haystack, match),
                    )

        return PillarResult(
            pillar="platform_scale", label=spec["label"], points=0,
            weight=weight, contribution=0.0,
            band="no scale signal", evidence=None,
        )

    @staticmethod
    def _evidence(haystack: str, match: re.Match[str]) -> str:
        """A short window around the match, so the reason is checkable."""
        start = max(0, match.start() - 24)
        end = min(len(haystack), match.end() + 24)
        snippet = haystack[start:end].strip()
        return f"...{snippet}..." if start > 0 else snippet

    def _classify(self, score: int) -> tuple[str, str]:
        for tier in self._tiers:
            if score >= tier["min_score"]:
                return tier["name"], tier["disposition"]
        last = self._tiers[-1]
        return last["name"], last["disposition"]


@lru_cache(maxsize=8)
def load_rubric(path: str | Path = DEFAULT_RUBRIC_PATH) -> IcpRubric:
    """Load and cache a rubric file."""
    config = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return IcpRubric(config)
