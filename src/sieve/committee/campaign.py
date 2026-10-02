"""The campaign as the models see it, read from Sieve's campaign file.

Only what is sold and to whom: never accounts, contacts or their titles, so
the committee is decided before any contact is looked at.

A campaign file may carry an explicit block:

    offering:
      offering: Cloud migration and managed cloud infrastructure services
      verticals: [Financial Services, Retail]
      offered_tech: [...]            # optional

Without one, the text is taken from the TAL rubric already in the file: its
description, the industry bands worth 85 points or more (who it targets) and
the legacy-stack bands worth 85 points or more (what technology it works on).
Nothing is added that the file does not say.
"""

from __future__ import annotations

from pathlib import Path

import yaml

EMPTY_CAMPAIGN = "Offering being sold: N/A"


def campaign_text(source: str | Path | dict) -> str:
    data = source if isinstance(source, dict) else yaml.safe_load(Path(source).read_text(encoding="utf-8"))
    block = data.get("offering")
    if isinstance(block, dict):
        lines = [f"Offering being sold: {block['offering']}"]
        if block.get("verticals"):
            lines.append(f"Target companies: {', '.join(block['verticals'])}")
        if block.get("offered_tech"):
            lines.append(f"Technology the offering works on: {', '.join(block['offered_tech'])}")
        return "\n".join(lines)

    lines = [f"Offering being sold: {data.get('description') or data.get('campaign')}"]
    pillars = data.get("pillars", {})
    targets = [b["label"] for b in pillars.get("industry_fit", {}).get("bands", [])
               if b.get("points", 0) >= 85 and b.get("label")]
    if targets:
        lines.append(f"Target companies: {'; '.join(targets)}")
    tech = [b["label"] for b in pillars.get("legacy_stack", {}).get("bands", [])
            if b.get("points", 0) >= 85 and b.get("label")]
    if tech:
        lines.append(f"Technology the offering works on: {'; '.join(tech)}")
    return "\n".join(lines)
