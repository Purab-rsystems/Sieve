"""Sieve — the pre-query and post-enrichment layer for the LeadDrip pipeline.

Public API:

    from sieve import normalize, load_tables
    result = normalize("ZoomDump.csv")
    result.report()
"""

from .io.writers.additive import PREFIX, attach_columns, enforce_additive
from .model.core import (
    Account, CanonicalBatch, Contact, Issue, IssueLevel, Provenance,
    Signal, SignalKind,
)
from .pipeline import (
    NormalizeResult, TableOutcome, accounts_to_frame, contacts_to_frame,
    load_tables, normalize, pair_by_source_row,
)
from .identity import account_id, contact_id
from .roster import (
    ROSTER_COLUMNS, RosterResult, assemble, designate, identify, run, score,
    validate,
)
from .scoring.icp import IcpRubric, IcpScore, load_rubric
from .taxonomy.designation import Designation, TitleTaxonomy, load_taxonomy
from .validation.engine import ContactUnderTest, Flag, validate_contact

__version__ = "1.0.0"

__all__ = [
    "__version__", "normalize", "load_tables", "NormalizeResult",
    "TableOutcome", "accounts_to_frame", "contacts_to_frame",
    "Account", "Contact", "Signal", "SignalKind", "Issue", "IssueLevel",
    "Provenance", "CanonicalBatch", "pair_by_source_row",
    "PREFIX", "attach_columns", "enforce_additive",
    "account_id", "contact_id",
    "identify", "designate", "score", "validate", "assemble", "run",
    "RosterResult", "ROSTER_COLUMNS",
    "IcpRubric", "IcpScore", "load_rubric",
    "Designation", "TitleTaxonomy", "load_taxonomy",
    "ContactUnderTest", "Flag", "validate_contact",
]
