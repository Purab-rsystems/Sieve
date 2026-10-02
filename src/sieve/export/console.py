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
/* Styled after DocQA: warm cocoa-brown surfaces, one soft honey accent, a
   rounded typeface (Nunito when installed; no font is fetched, the page
   stays offline), roomy padding and soft shadows. Layout only: the data,
   the keep/drop boxes and the approval file are unchanged. */
:root {
  --dq-bg:#1C1A17; --dq-side:#221F1C; --dq-card:#2A2622; --dq-you:#3B3229;
  --dq-line:#3A352F; --dq-text:#EFE8DF; --dq-muted:#B5AA9C; --dq-honey:#E9B872;
  --dq-honey-soft:rgba(233,184,114,0.16); --dq-shadow:0 6px 24px rgba(0,0,0,0.30);
  --dq-font:'Nunito','Segoe UI',system-ui,-apple-system,sans-serif;
  --ok:#9CC68B; --warn:#E9B872; --bad:#E39A86; --crit:#E39A86; --high:#E8AE7E;
  --med:#E9B872; --low:#B5AA9C; --sky:#9DBBD8;
}
* { box-sizing:border-box; }
html, body { background:var(--dq-bg); }
body { margin:0; color:var(--dq-text); font:15px/1.6 var(--dq-font);
  font-variant-numeric:tabular-nums; display:flex; min-height:100vh; }
code { background:var(--dq-you); padding:1px 6px; border-radius:6px; font-size:13px; }

/* sidebar, like DocQA's documents panel */
aside { width:300px; flex-shrink:0; background:var(--dq-side);
  border-right:1px solid var(--dq-line); padding:22px 18px; position:sticky;
  top:0; height:100vh; overflow-y:auto; display:flex; flex-direction:column; gap:16px; }
.brand { display:flex; align-items:center; gap:10px; }
.brand svg { width:36px; height:36px; }
.brand b { font-size:20px; font-weight:800; letter-spacing:-0.01em; }
.brand span { display:block; color:var(--dq-muted); font-size:12px; font-weight:600; }
aside h2 { font-size:1.05rem; font-weight:800; margin:6px 0 0; }
.tiles { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
.tile { background:var(--dq-card); border:1px solid var(--dq-line); border-radius:18px;
  padding:12px 14px; }
.tile .v { font-size:24px; font-weight:800; line-height:1.2; }
.tile .k { color:var(--dq-muted); font-size:12px; font-weight:600; }
.tile.hl .v { color:var(--dq-honey); }
.note { color:var(--dq-muted); font-size:13px; line-height:1.6; margin:0;
  border:1px solid var(--dq-line); border-left:3px solid var(--dq-honey);
  border-radius:12px; background:var(--dq-honey-soft); padding:10px 12px; }
.note b { color:var(--dq-honey); }
.spacer { flex:1; }
button { font:inherit; font-weight:700; padding:10px 18px; border-radius:999px;
  cursor:pointer; border:1px solid var(--dq-line); background:var(--dq-card);
  color:var(--dq-text); box-shadow:var(--dq-shadow); }
button.primary { background:var(--dq-honey); border-color:var(--dq-honey);
  color:var(--dq-bg); width:100%; font-size:15px; }
button.primary:hover { filter:brightness(1.06); }
button:focus-visible { outline:none; box-shadow:0 0 0 4px var(--dq-honey-soft), var(--dq-shadow); }
button:disabled { opacity:.5; cursor:not-allowed; }

/* main column */
main { flex:1; min-width:0; padding:40px 32px 60px; }
.inner { max-width:1100px; margin:0 auto; }
.hello { font-size:2rem; font-weight:800; letter-spacing:-0.01em; line-height:1.25; margin:0 0 6px; }
.promise { color:var(--dq-muted); margin:0 0 28px; font-size:1.02rem; }

/* an account: a soft card, like a DocQA answer */
.acct { background:var(--dq-card); border:1px solid var(--dq-line); border-radius:22px;
  margin-bottom:18px; overflow:hidden; box-shadow:var(--dq-shadow); }
.acct > summary { padding:16px 22px; cursor:pointer; display:flex; gap:12px;
  align-items:center; flex-wrap:wrap; list-style:none; }
.acct > summary::-webkit-details-marker { display:none; }
.acct > summary:hover .co { color:var(--dq-honey); }
.acct[open] > summary { border-bottom:1px solid var(--dq-line); }
.co { font-weight:800; font-size:17px; flex:1; min-width:180px; }
.chip { display:inline-block; padding:3px 12px; border-radius:999px; font-size:12.5px;
  font-weight:700; background:var(--dq-side); border:1px solid var(--dq-line); white-space:nowrap; }
.score { color:var(--dq-honey); border-color:var(--dq-honey); background:var(--dq-honey-soft); }
.tier { border-color:currentColor; }
.tA { color:var(--ok); } .tB { color:var(--warn); } .tC { color:var(--bad); }
.pill { font-size:13px; color:var(--dq-muted); }
.body { padding:14px 22px 18px; }
.pillars { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; }
.pillars span { font-size:12.5px; color:var(--dq-muted); background:var(--dq-side);
  border:1px solid var(--dq-line); border-radius:999px; padding:3px 12px; }
.pillars b { color:var(--dq-text); }
.wrap { overflow-x:auto; border:1px solid var(--dq-line); border-radius:14px; background:var(--dq-side); }
table { width:100%; border-collapse:collapse; font-size:14px; }
th { text-align:left; padding:10px 12px; color:var(--dq-muted); font-weight:700;
  font-size:12px; border-bottom:1px solid var(--dq-line); white-space:nowrap; }
td { padding:10px 12px; border-bottom:1px solid var(--dq-line); vertical-align:top; }
tbody tr:last-child td { border-bottom:none; }
tbody tr:hover td { background:rgba(233,184,114,0.05); }
tr.dropped td { opacity:.4; text-decoration:line-through; }
input[type=checkbox] { accent-color:var(--dq-honey); width:16px; height:16px; cursor:pointer; }
.seat { font-weight:700; font-size:13px; white-space:nowrap; }
.seat-eb { color:var(--dq-honey); } .seat-ch { color:var(--ok); }
.seat-te { color:var(--sky); } .seat-in { color:var(--high); }
.seat-un { color:var(--dq-muted); font-weight:400; }
.flag { display:inline-block; font-size:11px; font-weight:700; padding:2px 8px;
  border-radius:999px; border:1px solid currentColor; margin:1px 4px 2px 0; white-space:nowrap; }
.f-crit { color:var(--crit); } .f-high { color:var(--high); }
.f-med { color:var(--med); } .f-low { color:var(--low); }
.mono { font-variant-numeric:tabular-nums; }

@media (max-width: 860px) {
  body { display:block; }
  aside { width:auto; height:auto; position:static; border-right:none;
    border-bottom:1px solid var(--dq-line); }
  .tiles { grid-template-columns:repeat(4, 1fr); }
  main { padding:24px 16px 48px; }
  .hello { font-size:1.55rem; }
}
@media (max-width: 520px) { .tiles { grid-template-columns:1fr 1fr; } }
@media (prefers-reduced-motion: reduce) { * { transition:none !important; animation:none !important; } }
</style></head><body>
<aside>
  <div class="brand">
    <svg viewBox="0 0 48 48" aria-hidden="true">
      <path d="M6 16 H42 Q42 36 24 40 Q6 36 6 16 Z" fill="#2B2723" stroke="#EFE8DF" stroke-width="1.6" stroke-linejoin="round"/>
      <path d="M3 16 H45" stroke="#EFE8DF" stroke-width="2" stroke-linecap="round"/>
      <g fill="#E9B872"><circle cx="16" cy="22" r="1.6"/><circle cx="24" cy="22" r="1.6"/><circle cx="32" cy="22" r="1.6"/>
        <circle cx="20" cy="28" r="1.6"/><circle cx="28" cy="28" r="1.6"/><circle cx="24" cy="34" r="1.6"/></g>
      <path d="M24 41 V46" stroke="#E9B872" stroke-width="2" stroke-linecap="round"/>
      <circle cx="24" cy="8" r="3.2" fill="#E9B872"/>
    </svg>
    <div><b>Sieve</b><span>Review Console</span></div>
  </div>
  <h2>This roster</h2>
  <div class="tiles" id="totals"></div>
  <p class="note">Every row is <b>kept</b> by default. Untick a contact to drop
  it from the Salesforce sync. Approving downloads an approval file; pass it to
  <code>sieve sync --approval approval.json</code> to build the payload.</p>
  <div class="spacer"></div>
  <button id="approve" class="primary">Approve Roster</button>
</aside>
<main><div class="inner">
  <p class="hello">Review the roster</p>
  <p class="promise">Accounts by TAL score, flagged ones open. Check each committee, keep or drop each contact, then approve.</p>
  <div id="list"></div>
</div></main>
<script>
const DATA = __DATA__;
const SEV = {ROLE_DEPARTED:'crit', DOMAIN_ANOMALY:'crit', GENERIC_EMAIL:'high',
  TITLE_MISMATCH:'med', DISPOSABLE_PHONE:'med', SENIORITY_AMBIGUITY:'low'};
const SEAT = {'Economic Buyer':'seat-eb', 'Champion':'seat-ch',
  'Technical Evaluator':'seat-te', 'Influencer':'seat-in', 'Unassigned':'seat-un'};
const dropped = new Set();

function esc(s){ const d=document.createElement('div'); d.textContent=s??'';
  return d.innerHTML; }

function render(){
  const t = DATA.totals;
  document.getElementById('totals').innerHTML = [
    [t.accounts, 'accounts', ''], [t.contacts, 'contacts', ''],
    [t.flagged, 'flagged', 'hl'], [t.complete_committees, 'complete committees', '']
  ].map(([v, k, c]) => `<div class="tile ${c}"><div class="v">${v}</div><div class="k">${k}</div></div>`).join('');

  document.getElementById('list').innerHTML = DATA.accounts.map(a => `
    <details class="acct" ${a.flagged ? 'open' : ''}>
      <summary>
        <span class="co">${esc(a.company)}</span>
        <span class="chip tier t${a.tier.slice(-1)}">${esc(a.tier)}</span>
        <span class="chip score">TAL ${a.icp_score}</span>
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
