"""The additive-output guarantee lives here.

Every capability in Sieve returns the caller's data with extra columns
appended. This module is the single place that performs that merge, so the
guarantee holds in one place rather than in six.

Three rules hold on every write:

1. Input columns keep their values, their spelling and their original order.
2. Added columns carry the configured prefix and land after the input columns.
3. Row count and row order are preserved exactly.

`enforce_additive` checks all three against the original frame and raises when
any of them breaks, which turns a silent regression into a failing test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

__all__ = [
    "PREFIX",
    "AdditiveContractError",
    "attach_columns",
    "enforce_additive",
    "write_table",
]

PREFIX = "sv_"


class AdditiveContractError(Exception):
    """Raised when an operation would modify or drop caller data."""


def attach_columns(
    frame: pd.DataFrame,
    columns: Mapping[str, Sequence[Any]],
    *,
    prefix: str = PREFIX,
) -> pd.DataFrame:
    """Return a copy of `frame` with `columns` appended.

    Column names are prefixed when they do not already carry the prefix.
    Every sequence must match the frame's length.
    """
    result = frame.copy()
    for name, values in columns.items():
        if len(values) != len(frame):
            raise AdditiveContractError(
                f"Column {name!r} has {len(values)} values for "
                f"{len(frame)} rows."
            )
        key = name if name.startswith(prefix) else f"{prefix}{name}"
        if key in frame.columns:
            raise AdditiveContractError(
                f"Column {key!r} already exists in the input. "
                f"Sieve never overwrites caller data."
            )
        result[key] = list(values)
    enforce_additive(frame, result, prefix=prefix)
    return result


def enforce_additive(
    original: pd.DataFrame,
    produced: pd.DataFrame,
    *,
    prefix: str = PREFIX,
) -> None:
    """Verify that `produced` only adds to `original`."""
    if len(original) != len(produced):
        raise AdditiveContractError(
            f"Row count changed from {len(original)} to {len(produced)}."
        )

    original_columns = list(original.columns)
    kept = [c for c in produced.columns if c in original_columns]
    if kept != original_columns:
        missing = [c for c in original_columns if c not in produced.columns]
        if missing:
            raise AdditiveContractError(f"Input columns dropped: {missing}")
        raise AdditiveContractError(
            "Input column order changed. Expected "
            f"{original_columns[:5]}..., produced {kept[:5]}..."
        )

    added = [c for c in produced.columns if c not in original_columns]
    unprefixed = [c for c in added if not str(c).startswith(prefix)]
    if unprefixed:
        raise AdditiveContractError(
            f"Added columns must start with {prefix!r}: {unprefixed}"
        )

    for column in original_columns:
        left = original[column].reset_index(drop=True)
        right = produced[column].reset_index(drop=True)
        if not left.equals(right):
            differing = int((left.astype("object") != right.astype("object")).sum())
            raise AdditiveContractError(
                f"Input column {column!r} was modified in {differing} rows."
            )


def write_table(frame: pd.DataFrame, path: Path) -> Path:
    """Write a frame to CSV or XLSX, chosen by the path's extension."""
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        frame.to_excel(path, index=False)
    elif suffix == ".tsv":
        frame.to_csv(path, index=False, sep="\t")
    else:
        frame.to_csv(path, index=False)
    return path
