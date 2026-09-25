"""Command-line interface.

Every command wraps the same functions the Python API exposes, so the two
never drift apart. Files get written only when --out is supplied.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

import pandas as pd

from .io.adapters.base import default_registry as adapter_registry
from .io.writers.additive import write_table
from .pipeline import accounts_to_frame, contacts_to_frame, load_tables, normalize
from .roster import assemble as assemble_stage
from .roster import designate as designate_stage
from .roster import identify as identify_stage
from .roster import run as run_stage
from .roster import score as score_stage
from .roster import validate as validate_stage

app = typer.Typer(
    add_completion=False,
    help="Sieve — pre-query and post-enrichment layer for the LeadDrip pipeline.",
)


@app.command("normalize")
def normalize_command(
    inp: list[Path] = typer.Option(..., "--in", help="Source file(s) to read."),
    source: str | None = typer.Option(
        None, "--source", help="Force an adapter. Omit to auto-detect."
    ),
    out_accounts: Path | None = typer.Option(None, "--out-accounts"),
    out_contacts: Path | None = typer.Option(None, "--out-contacts"),
    report: Path | None = typer.Option(None, "--report"),
    strict: bool = typer.Option(
        False, "--strict", help="Raise when a table matches no adapter."
    ),
) -> None:
    """Map raw dumps onto the canonical account and contact model."""
    result = normalize(list(inp), source=source, strict=strict)

    if out_accounts:
        write_table(accounts_to_frame(result), out_accounts)
    if out_contacts:
        write_table(contacts_to_frame(result), out_contacts)

    payload = result.report()
    if report:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(payload, indent=2, default=str))

    totals = payload["totals"]
    typer.echo(
        f"{totals['tables_recognised']}/{totals['tables']} tables recognised, "
        f"{totals['rows']} rows read, "
        f"{totals['accounts']} accounts and {totals['contacts']} contacts produced, "
        f"{payload['issues']['total']} issues."
    )
    for outcome in payload["tables"]:
        status = outcome["source_id"] or "UNRECOGNISED"
        typer.echo(
            f"  {outcome['table']:<45} {status:<16} "
            f"conf={outcome['confidence']:<5} rows={outcome['rows']}"
        )


@app.command("inspect")
def inspect_command(
    inp: Path = typer.Option(..., "--in", help="File to inspect."),
    show_columns: bool = typer.Option(False, "--columns"),
) -> None:
    """Report which adapter each table in a file scores against."""
    registry = adapter_registry()
    for table in load_tables(inp):
        typer.echo(f"\n{table.name}  ({len(table)} rows, {len(table.columns)} columns)")
        for source_id, score in registry.score_all(table)[:5]:
            marker = "*" if score >= 0.55 else " "
            typer.echo(f"  {marker} {source_id:<20} {score:.3f}")
        if show_columns:
            typer.echo(f"  columns: {', '.join(table.columns)}")


# --------------------------------------------------------------------------
# Sieve v1.0 stages
# --------------------------------------------------------------------------

def _read_frame(path: Path) -> pd.DataFrame:
    """Read a roster file, keeping 'NA' as the string it is."""
    if path.suffix.lower() in {".xlsx", ".xlsm"}:
        return pd.read_excel(path, keep_default_na=False, na_values=[""])
    return pd.read_csv(path, keep_default_na=False, na_values=[""])


@app.command("score")
def score_command(
    inp: Path = typer.Option(..., "--in", help="Format-DB file to score."),
    out: Path = typer.Option(..., "--out", help="Where to write the result."),
    rubric: Path | None = typer.Option(None, "--rubric", help="Rubric YAML."),
) -> None:
    """Gate 1: deterministic ICP scoring (module M3)."""
    from .scoring.icp import load_rubric

    frame = _read_frame(inp)
    result = score_stage(frame, rubric=load_rubric(rubric) if rubric else None)
    write_table(result, out)
    counts = result["sv_target_tier"].value_counts().to_dict()
    typer.echo(f"Scored {len(result)} rows. Tiers: {counts}")


@app.command("validate")
def validate_command(
    inp: Path = typer.Option(..., "--in"),
    out: Path = typer.Option(..., "--out"),
) -> None:
    """Gate 2: contact validation and anomaly flagging (module M4)."""
    frame = _read_frame(inp)
    result = validate_stage(designate_stage(frame))
    write_table(result, out)
    flagged = int((result["sv_validation_status"] != "VERIFIED").sum())
    typer.echo(
        f"Validated {len(result)} rows. {flagged} carry flags, "
        f"mean confidence {result['sv_confidence'].mean():.3f}."
    )


@app.command("designate")
def designate_command(
    inp: Path = typer.Option(..., "--in"),
    out: Path = typer.Option(..., "--out"),
) -> None:
    """Normalize titles and derive seniority and authority (module M6)."""
    frame = _read_frame(inp)
    result = designate_stage(frame)
    write_table(result, out)
    resolved = int(result["sv_canonical_title"].notna().sum())
    typer.echo(f"Resolved {resolved}/{len(result)} titles.")


@app.command("run")
def run_command(
    inp: Path = typer.Option(..., "--in", help="Format-DB file from LeadDrip."),
    out: Path = typer.Option(
        Path("verified_prospect_roster.csv"), "--out",
        help="Terminal roster output.",
    ),
    audit: Path | None = typer.Option(
        None, "--audit", help="Path for sieve_audit_log.jsonl."
    ),
    report: Path | None = typer.Option(None, "--report"),
    run_id: str | None = typer.Option(None, "--run-id"),
    all_tiers: bool = typer.Option(
        False, "--all-tiers",
        help="Assemble committees for every tier, not only Tier A.",
    ),
) -> None:
    """Run the whole pipeline, M0 through M8."""
    frame = _read_frame(inp)
    result = run_stage(
        frame, audit_path=audit, run_id=run_id, tier_a_only=not all_tiers
    )
    result.to_csv(out)

    if report:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(result.summary, indent=2, default=str))

    summary = result.summary
    typer.echo(
        f"{summary['rows']} rows across {summary['accounts']} accounts.\n"
        f"  Tiers:      {summary['tiers']}\n"
        f"  Validation: {summary['validation_status']}\n"
        f"  Committee:  {summary['committee_roles']}\n"
        f"  Flags:      {summary['flags']}\n"
        f"  Titles resolved: {summary['titles_resolved']}/{summary['rows']}, "
        f"mean confidence {summary['mean_confidence']}"
    )
    typer.echo(f"Roster written to {out}")


# --------------------------------------------------------------------------
# M8: review console, CRM sync, dial queue
# --------------------------------------------------------------------------

@app.command("console")
def console_command(
    inp: Path = typer.Option(..., "--in", help="verified_prospect_roster.csv"),
    out: Path = typer.Option(
        Path("review_console.html"), "--out", help="Console page to write."
    ),
    tiers: str = typer.Option(
        "Tier A,Tier B", "--tiers", help="Comma-separated tiers to review."
    ),
) -> None:
    """Render the HITL review console (module M8, step i)."""
    from .export.console import write_console

    frame = _read_frame(inp)
    tier_filter = tuple(t.strip() for t in tiers.split(",") if t.strip())
    path = write_console(frame, out, tier_filter=tier_filter)
    typer.echo(f"Review console written to {path}")
    typer.echo("Open it, work through the flagged rows, then click Approve Roster.")


@app.command("sync")
def sync_command(
    inp: Path = typer.Option(..., "--in", help="verified_prospect_roster.csv"),
    out: Path = typer.Option(
        Path("salesforce"), "--out", help="Directory for the upsert payloads."
    ),
    approval: Path | None = typer.Option(
        None, "--approval", help="approval.json from the review console."
    ),
    tiers: str = typer.Option("Tier A,Tier B", "--tiers"),
    force: bool = typer.Option(
        False, "--force", help="Stage without a sign-off, for a dry run."
    ),
) -> None:
    """Stage the Salesforce Bulk API payloads (module M8, step iii)."""
    from .export.salesforce import build_payload, load_approval

    if approval is None and not force:
        typer.echo(
            "No approval file supplied. The PRD requires a rep sign-off before "
            "CRM write-back.\n"
            "  Run `sieve console --in <roster>` and approve, then pass "
            "--approval approval.json.\n"
            "  Use --force to stage an unapproved dry run."
        )
        raise typer.Exit(code=1)

    frame = _read_frame(inp)
    payload = build_payload(
        frame,
        approval=load_approval(approval) if approval else None,
        approved=bool(approval),
        tier_filter=tuple(t.strip() for t in tiers.split(",") if t.strip()),
    )
    paths = payload.write(out)
    manifest = payload.manifest
    typer.echo(
        f"Staged {manifest['accounts']} accounts and {manifest['contacts']} "
        f"contacts for upsert.\n"
        f"  Held back: {manifest['held_back']} {manifest['held_back_reasons']}\n"
        f"  Committee: {manifest['committee_seats']}\n"
        f"  Approved:  {manifest['approved']}"
    )
    for name, path in paths.items():
        typer.echo(f"  {name:10} {path}")


@app.command("queue")
def queue_command(
    inp: Path = typer.Option(..., "--in", help="verified_prospect_roster.csv"),
    out: Path = typer.Option(
        Path("dial_queue"), "--out", help="Directory for the queue files."
    ),
    tiers: str = typer.Option("Tier A", "--tiers"),
    all_contacts: bool = typer.Option(
        False, "--all-contacts",
        help="Queue every qualified contact, not only committee members.",
    ),
    min_confidence: float = typer.Option(0.60, "--min-confidence"),
) -> None:
    """Provision the Compass Max dial queue (module M8, step iv)."""
    from .export.queue import build_queue, write_queue

    frame = _read_frame(inp)
    queue = build_queue(
        frame,
        tier_filter=tuple(t.strip() for t in tiers.split(",") if t.strip()),
        committee_only=not all_contacts,
        min_confidence=min_confidence,
    )
    paths = write_queue(queue, out)
    typer.echo(f"Queued {len(queue)} phone-verified contacts.")
    if len(queue):
        typer.echo(
            f"  Priority range: {queue['sv_dial_priority'].min():.1f} to "
            f"{queue['sv_dial_priority'].max():.1f}"
        )
        typer.echo(f"  Numbers: {queue['number_type'].value_counts().to_dict()}")
    for name, path in paths.items():
        typer.echo(f"  {name:8} {path}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
