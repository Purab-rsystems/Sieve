"""Sieve Review Console (PRD module M8, steps i and ii).

Renders the assembled accounts and their committees as a self-contained HTML
page. A sales rep works through the flagged items, keeps or drops each one,
and clicks Approve Roster, which downloads an approval JSON.

That file is the sign-off gate: `sieve sync --approval approval.json` refuses
to build a Salesforce payload without it, which is what makes the
human-in-the-loop step load-bearing rather than decorative.

The page carries no external dependencies and makes no network calls, so it
opens from disk on any machine.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

import pandas as pd

__all__ = ["build_console", "write_console", "roster_to_review_model"]


def _clean(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value)


def roster_to_review_model(
    roster: pd.DataFrame,
    *,
    tier_filter: tuple[str, ...] = ("Tier A", "Tier B"),
) -> dict[str, Any]:
    """Group a roster into the accounts-and-committees shape the console shows."""
    frame = roster[roster["sv_target_tier"].isin(tier_filter)]
    accounts = []

    for account_id, group in frame.groupby("sv_account_id", sort=False):
        first = group.iloc[0]
        members = []
        for _, row in group.iterrows():
            flags = [f for f in _clean(row.get("sv_flags")).split(",") if f]
            members.append({
                "contact_id": _clean(row["sv_contact_id"]),
                "name": " ".join(
                    p for p in (_clean(row.get("First Name")),
                                _clean(row.get("Last Name"))) if p
                ),
                "title": _clean(row.get("Title")),
                "canonical": _clean(row.get("sv_canonical_title")),
                "seniority": _clean(row.get("sv_seniority")),
                "seat": _clean(row.get("sv_committee_role")) or "Unassigned",
                "email": _clean(row.get("Email")),
                "flags": flags,
                "confidence": float(row.get("sv_confidence") or 0),
                "status": _clean(row.get("sv_validation_status")),
                "priority": float(row.get("sv_dial_priority") or 0),
            })

        seats = {m["seat"] for m in members}
        members.sort(key=lambda m: (-m["priority"], m["contact_id"]))
        accounts.append({
            "account_id": _clean(account_id),
            "company": _clean(first.get("Company")),
            "website": _clean(first.get("Website URL") or first.get("Website")),
            "tier": _clean(first.get("sv_target_tier")),
            "icp_score": int(first.get("sv_icp_score") or 0),
            "breakdown": json.loads(_clean(first.get("sv_score_breakdown")) or "[]"),
            "complete": {"Economic Buyer", "Champion"}.issubset(seats),
            "members": members,
            "flagged": sum(1 for m in members if m["flags"]),
        })

    accounts.sort(key=lambda a: (-a["icp_score"], a["company"]))
    return {
        "accounts": accounts,
        "totals": {
            "accounts": len(accounts),
            "contacts": sum(len(a["members"]) for a in accounts),
            "flagged": sum(a["flagged"] for a in accounts),
            "complete_committees": sum(1 for a in accounts if a["complete"]),
        },
    }


_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sieve Review Console</title>
<style>
:root {
  --bg:#f6f7f9; --panel:#fff; --ink:#15181d; --muted:#616a76; --line:#dfe3e8;
  --a:#1c7a4a; --b:#8a6d1f; --c:#7a2020; --crit:#b3261e; --high:#a6541c;
  --med:#8a6d1f; --low:#4a5561; --accent:#1f4f8f;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg:#14171b; --panel:#1c2026; --ink:#e8eaed; --muted:#9aa4b0; --line:#2c323a;
  --a:#5fd39a; --b:#dcc06a; --c:#e08b8b; --crit:#f2938c; --high:#e8ab7a;
  --med:#dcc06a; --low:#9aa4b0; --accent:#7fb0f0;
} }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5
  ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
header { position:sticky; top:0; background:var(--panel);
  border-bottom:1px solid var(--line); padding:14px 20px; z-index:10;
  display:flex; gap:20px; align-items:center; flex-wrap:wrap; }
h1 { font-size:17px; margin:0; font-weight:600; }
.totals { color:var(--muted); font-size:13px; }
.totals b { color:var(--ink); }
main { padding:20px; max-width:1180px; margin:0 auto; }
.acct { background:var(--panel); border:1px solid var(--line);
  border-radius:8px; margin-bottom:14px; overflow:hidden; }
.acct > summary { padding:14px 16px; cursor:pointer; display:flex;
  gap:14px; align-items:center; flex-wrap:wrap; list-style:none; }
.acct > summary::-webkit-details-marker { display:none; }
.co { font-weight:600; flex:1; min-width:180px; }
.score { font-variant-numeric:tabular-nums; font-weight:600; }
.tier { padding:2px 9px; border-radius:999px; font-size:12px; font-weight:600;
  border:1px solid currentColor; }
.tA { color:var(--a); } .tB { color:var(--b); } .tC { color:var(--c); }
.pill { font-size:12px; color:var(--muted); }
.body { padding:0 16px 16px; border-top:1px solid var(--line); }
table { width:100%; border-collapse:collapse; font-size:14px; }
th { text-align:left; padding:9px 8px; color:var(--muted); font-weight:500;
  font-size:12px; text-transform:uppercase; letter-spacing:.03em;
  border-bottom:1px solid var(--line); }
td { padding:9px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
tr.dropped td { opacity:.4; text-decoration:line-through; }
.seat { font-weight:600; font-size:13px; }
.seat-eb { color:var(--a); } .seat-ch { color:var(--accent); }
.seat-in { color:var(--b); } .seat-un { color:var(--muted); font-weight:400; }
.flag { display:inline-block; font-size:11px; padding:1px 6px; border-radius:4px;
  border:1px solid currentColor; margin:1px 3px 1px 0; white-space:nowrap; }
.f-crit { color:var(--crit); } .f-high { color:var(--high); }
.f-med { color:var(--med); } .f-low { color:var(--low); }
.mono { font-variant-numeric:tabular-nums; }
.wrap { overflow-x:auto; }
button { font:inherit; padding:8px 16px; border-radius:6px; cursor:pointer;
  border:1px solid var(--line); background:var(--panel); color:var(--ink); }
button.primary { background:var(--accent); border-color:var(--accent);
  color:#fff; font-weight:600; }
button:disabled { opacity:.5; cursor:not-allowed; }
.pillars { margin-top:10px; font-size:12px; color:var(--muted); }
.pillars span { margin-right:14px; }
.note { color:var(--muted); font-size:13px; margin:0 0 16px; }
</style></head><body>
<header>
  <h1>Sieve Review Console</h1>
  <div class="totals" id="totals"></div>
  <div style="flex:1"></div>
  <button id="approve" class="primary">Approve Roster</button>
</header>
<main>
  <p class="note">Every row below is kept by default. Untick a contact to drop
  it from the Salesforce sync. Approving downloads an approval file; pass it to
  <code>sieve sync --approval approval.json</code> to build the payload.</p>
  <div id="list"></div>
</main>
<script>
const DATA = __DATA__;
const SEV = {ROLE_DEPARTED:'crit', DOMAIN_ANOMALY:'crit', GENERIC_EMAIL:'high',
  TITLE_MISMATCH:'med', DISPOSABLE_PHONE:'med', SENIORITY_AMBIGUITY:'low'};
const SEAT = {'Economic Buyer':'seat-eb', 'Champion':'seat-ch',
  'Influencer':'seat-in', 'Unassigned':'seat-un'};
const dropped = new Set();

function esc(s){ const d=document.createElement('div'); d.textContent=s??'';
  return d.innerHTML; }

function render(){
  const t = DATA.totals;
  document.getElementById('totals').innerHTML =
    `<b>${t.accounts}</b> accounts &middot; <b>${t.contacts}</b> contacts &middot;
     <b>${t.flagged}</b> flagged &middot; <b>${t.complete_committees}</b> complete committees`;

  document.getElementById('list').innerHTML = DATA.accounts.map(a => `
    <details class="acct" ${a.flagged ? 'open' : ''}>
      <summary>
        <span class="co">${esc(a.company)}</span>
        <span class="tier t${a.tier.slice(-1)}">${esc(a.tier)}</span>
        <span class="score">ICP ${a.icp_score}</span>
        <span class="pill">${a.members.length} contacts${a.flagged ?
          ` &middot; ${a.flagged} flagged` : ''}${a.complete ?
          ' &middot; committee complete' : ''}</span>
      </summary>
      <div class="body">
        <div class="pillars">${a.breakdown.map(p =>
          `<span>${esc(p.pillar)}: <b>${p.points}</b></span>`).join('')}</div>
        <div class="wrap"><table>
          <thead><tr><th>Keep</th><th>Seat</th><th>Name</th><th>Title</th>
          <th>Email</th><th>Flags</th><th>Conf</th><th>Priority</th></tr></thead>
          <tbody>${a.members.map(m => `
            <tr data-id="${esc(m.contact_id)}" class="${dropped.has(m.contact_id)
              ? 'dropped' : ''}">
              <td><input type="checkbox" ${dropped.has(m.contact_id) ? '' : 'checked'}
                data-id="${esc(m.contact_id)}"></td>
              <td class="seat ${SEAT[m.seat] || 'seat-un'}">${esc(m.seat)}</td>
              <td>${esc(m.name)}</td>
              <td>${esc(m.canonical || m.title)}</td>
              <td>${esc(m.email)}</td>
              <td>${m.flags.map(f =>
                `<span class="flag f-${SEV[f] || 'low'}">${esc(f)}</span>`).join('')}</td>
              <td class="mono">${m.confidence.toFixed(2)}</td>
              <td class="mono">${m.priority.toFixed(1)}</td>
            </tr>`).join('')}</tbody>
        </table></div>
      </div>
    </details>`).join('');

  document.querySelectorAll('input[type=checkbox]').forEach(box => {
    box.onchange = () => {
      const id = box.dataset.id;
      box.checked ? dropped.delete(id) : dropped.add(id);
      box.closest('tr').classList.toggle('dropped', !box.checked);
    };
  });
}

document.getElementById('approve').onclick = () => {
  const approved = [];
  DATA.accounts.forEach(a => a.members.forEach(m => {
    if (!dropped.has(m.contact_id)) approved.push(m.contact_id);
  }));
  const payload = {
    approved: true,
    approved_at: new Date().toISOString(),
    approved_contact_ids: approved,
    dropped_contact_ids: [...dropped],
    totals: {approved: approved.length, dropped: dropped.size}
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)],
    {type:'application/json'});
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = 'approval.json';
  link.click();
  URL.revokeObjectURL(link.href);
};

render();
</script></body></html>
"""


def build_console(model: dict[str, Any]) -> str:
    """Render the review console HTML from a review model."""
    payload = json.dumps(model, separators=(",", ":"), default=str)
    # Guard against a '</script>' inside any free-text field closing the block.
    payload = payload.replace("</", "<\\/")
    return _PAGE.replace("__DATA__", payload)


def write_console(
    roster: pd.DataFrame,
    path: Path | str,
    *,
    tier_filter: tuple[str, ...] = ("Tier A", "Tier B"),
) -> Path:
    """Build the console for a roster and write it to disk."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    model = roster_to_review_model(roster, tier_filter=tier_filter)
    path.write_text(build_console(model), encoding="utf-8")
    return path
