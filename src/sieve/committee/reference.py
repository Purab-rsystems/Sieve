"""The role reference sheet and the committee rules.

role_reference.json lists every committee title Sieve knows: its function,
level (C, C-1, C-2), the published description copied word for word with its
source, and the regexes that tell which raw contact titles hold it.
rules.yaml holds the role minimums, the seat order and the role definitions,
quoted from their sources.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config" / "committee"
REFERENCE_PATH = CONFIG_DIR / "role_reference.json"
RULES_PATH = CONFIG_DIR / "rules.yaml"

LEVELS = ("C", "C-1", "C-2")


@dataclass(frozen=True)
class TitleDef:
    title: str
    function: str
    level: str
    summary: str
    duties: tuple[str, ...]
    source: str
    url: str
    approximate: bool
    patterns: tuple[re.Pattern, ...]


@lru_cache(maxsize=None)
def load_reference(path: str | None = None) -> tuple[TitleDef, ...]:
    data = json.loads(Path(path or REFERENCE_PATH).read_text(encoding="utf-8"))
    out = []
    for t in data["titles"]:
        out.append(TitleDef(
            title=t["title"], function=t["function"], level=t["level"],
            summary=t.get("summary", ""), duties=tuple(t.get("duties", [])),
            source=t.get("source", ""), url=t.get("url", ""),
            approximate=bool(t.get("approximate")),
            patterns=tuple(re.compile(p) for p in t["patterns"]),
        ))
    return tuple(out)


@lru_cache(maxsize=None)
def _exclude(path: str | None = None) -> re.Pattern:
    data = json.loads(Path(path or REFERENCE_PATH).read_text(encoding="utf-8"))
    return re.compile(data.get("exclude") or r"$^")


@lru_cache(maxsize=None)
def load_rules(path: str | None = None) -> dict:
    return yaml.safe_load(Path(path or RULES_PATH).read_text(encoding="utf-8"))


def functions(reference: tuple[TitleDef, ...] | None = None) -> dict[str, list[TitleDef]]:
    """Function -> its titles, most senior first."""
    ref = reference or load_reference()
    out: dict[str, list[TitleDef]] = {}
    for t in ref:
        out.setdefault(t.function, []).append(t)
    for titles in out.values():
        titles.sort(key=lambda t: LEVELS.index(t.level))
    return out


def by_title(reference: tuple[TitleDef, ...] | None = None) -> dict[str, TitleDef]:
    return {t.title: t for t in (reference or load_reference())}


def normalize_title(raw: str) -> str:
    s = str(raw or "").lower()
    s = s.replace("&", " and ").replace("v.p.", "vp")
    s = re.sub(r"\bvice[- ]president\b", "vp", s)
    s = re.sub(r"\b(senior|sr\.?|executive|exec\.?|group|regional|global|corporate|associate|assistant)\s+vp\b", "vp", s)
    s = re.sub(r"\b(svp|evp|avp|gvp)\b", "vp", s)
    s = re.sub(r"[,/|;:()\-–—]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def holds(raw_title: str, reference: tuple[TitleDef, ...] | None = None) -> list[str]:
    """Library titles a raw contact title holds (possibly several, possibly none)."""
    if not raw_title or not str(raw_title).strip():
        return []
    ref = reference or load_reference()
    s = normalize_title(raw_title)          # "Assistant VP" is a VP rank, so normalise first
    if _exclude().search(s):
        return []
    return [t.title for t in ref if any(p.search(s) for p in t.patterns)]
