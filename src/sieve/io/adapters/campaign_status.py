"""Campaign status sheet.

The account-level intelligence layer. This is the only source carrying signal
text alongside a human-assigned intent tier, which makes it the training and
evaluation set for module M3.

Four columns hold signal text, each mapping to a different signal kind. The
column headings carry embedded newlines in the workbook, which
`canonical_header` collapses before comparison.

Two static helpers at the bottom expose the label columns that M3 and M1 will
need: `intent_labels` returns the human High/Medium call per row, and
`recommended_roles` returns the target roles a human chose per account.
"""

from __future__ import annotations

from ...model.core import Account, CanonicalBatch, Signal, SignalKind
from ...normalize.values import clean_str, normalize_company_name, normalize_domain
from ..readers.base import RawTable
from .base import ColumnResolver, RowReader, SourceAdapter

__all__ = ["CampaignStatusAdapter"]

#: Column heading -> the kind of signal it holds.
_SIGNAL_COLUMNS: tuple[tuple[str, SignalKind], ...] = (
    ("Legacy Stack Signals", SignalKind.LEGACY_STACK),
    ("Modernization Intent Signal", SignalKind.MODERNIZATION_INTENT),
    ("Estimated Scale (Users / Revenue Tier)", SignalKind.SCALE),
    ("Pitch Angle for EXIQO Case Study", SignalKind.PITCH_ANGLE),
)


class CampaignStatusAdapter(SourceAdapter):
    source_id = "campaign_status"
    signature_columns = (
        "Company Name", "Website", "HQ Location", "Region", "Category",
        "Platform / Product Description", "Legacy Stack Signals",
        "Modernization Intent Signal", "Pitch Angle for EXIQO Case Study",
        "Recommended Target Roles",
    )
    required_columns = ("Recommended Target Roles",)

    def to_canonical(self, table: RawTable) -> CanonicalBatch:
        batch = self._new_batch(table)
        resolver = ColumnResolver(table.columns)

        for index, row in enumerate(table.rows, start=1):
            read = self._reader(row, resolver, batch, index)

            company_raw = read.get("Company Name")
            if company_raw is None:
                continue        # a trailing or blank row

            account = Account(
                source_row=index,
                source_ref=f"account:{index}",
                name_raw=company_raw,
                name=normalize_company_name(company_raw),
                domain=read.get("Website", "Website.1", parse=normalize_domain),
                hq_city=read.get("HQ Location"),
                region=read.get("Region"),      # 'NA' here means North America
                industry=read.get("Category"),
                signals=self._signals(read),
                source_status=read.get("Status"),
            )
            batch.accounts.append(account)
            batch.record_provenance(account.source_ref, read.take())

        return batch

    @staticmethod
    def _signals(read: RowReader) -> list[Signal]:
        """Pull each signal column into a typed Signal, skipping empty ones."""
        signals = []
        for heading, kind in _SIGNAL_COLUMNS:
            text = read.get(heading)
            if text:
                signals.append(Signal(kind=kind, text=text))
        return signals

    # -- label columns for the M1 and M3 eval harnesses ----------------------

    @staticmethod
    def intent_labels(table: RawTable) -> list[str | None]:
        """The human-assigned intent tier per row: High, Medium or Emerging."""
        resolver = ColumnResolver(table.columns)
        column = resolver.resolve([
            "Intent Score (High / Medium / Emerging)", "Intent Score",
        ])
        if column is None:
            return []
        return [clean_str(row.get(column)) for row in table.rows]

    @staticmethod
    def recommended_roles(table: RawTable) -> list[list[str]]:
        """Target roles per row.

        The cell holds several roles separated by newlines, and each line may
        hold alternatives separated by slashes, as in
        'CTO / VP of Engineering'. Both separators get split.
        """
        resolver = ColumnResolver(table.columns)
        column = resolver.resolve(["Recommended Target Roles"])
        if column is None:
            return []

        per_row = []
        for row in table.rows:
            roles = []
            for line in str(row.get(column) or "").splitlines():
                for alternative in line.split("/"):
                    role = clean_str(alternative)
                    if role:
                        roles.append(role)
            per_row.append(roles)
        return per_row
