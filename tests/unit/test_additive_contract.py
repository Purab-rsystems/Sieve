"""Tests for the additive-output guarantee.

This contract is the reason the component can be dropped into someone else's
pipeline, so it is tested harder than anything else in the codebase.
"""

from __future__ import annotations

import pandas as pd
import pytest

from sieve.io.writers.additive import (
    AdditiveContractError, attach_columns, enforce_additive,
)


@pytest.fixture
def frame() -> pd.DataFrame:
    return pd.DataFrame({
        "First Name": ["Isaac", "Shyam", "Eric"],
        "Title": ["Chief Product Officer", "CTO", "CEO"],
        "Email": ["a@x.com", "b@x.com", "c@x.com"],
    })


class TestAttachColumns:
    def test_appends_with_prefix(self, frame):
        out = attach_columns(frame, {"canonical_role": ["cpo", "cto", "ceo"]})
        assert list(out.columns) == [
            "First Name", "Title", "Email", "sv_canonical_role"
        ]

    def test_already_prefixed_name_is_not_doubled(self, frame):
        out = attach_columns(frame, {"sv_flags": ["", "", ""]})
        assert "sv_flags" in out.columns and "sv_sv_flags" not in out.columns

    def test_input_frame_is_untouched(self, frame):
        before = frame.copy(deep=True)
        attach_columns(frame, {"score": [1, 2, 3]})
        pd.testing.assert_frame_equal(frame, before)

    def test_length_mismatch_raises(self, frame):
        with pytest.raises(AdditiveContractError, match="2 values for 3 rows"):
            attach_columns(frame, {"score": [1, 2]})

    def test_a_name_matching_an_input_column_is_prefixed_and_kept_separate(self, frame):
        """The prefix is what makes collision impossible, so this is legal."""
        out = attach_columns(frame, {"Title": ["x", "y", "z"]})
        assert list(out["Title"]) == list(frame["Title"])
        assert list(out["sv_Title"]) == ["x", "y", "z"]

    def test_overwriting_an_existing_sv_column_raises(self, frame):
        seeded = attach_columns(frame, {"score": [1, 2, 3]})
        with pytest.raises(AdditiveContractError, match="never overwrites"):
            attach_columns(seeded, {"score": [4, 5, 6]})


class TestEnforceAdditive:
    def test_accepts_a_pure_addition(self, frame):
        produced = frame.copy()
        produced["sv_score"] = [1, 2, 3]
        enforce_additive(frame, produced)

    def test_rejects_a_dropped_column(self, frame):
        with pytest.raises(AdditiveContractError, match="dropped"):
            enforce_additive(frame, frame.drop(columns=["Email"]))

    def test_rejects_a_modified_value(self, frame):
        produced = frame.copy()
        produced.loc[0, "Title"] = "Chief Product Officer (edited)"
        with pytest.raises(AdditiveContractError, match="modified in 1 rows"):
            enforce_additive(frame, produced)

    def test_rejects_reordered_columns(self, frame):
        with pytest.raises(AdditiveContractError, match="order changed"):
            enforce_additive(frame, frame[["Title", "First Name", "Email"]])

    def test_rejects_a_dropped_row(self, frame):
        with pytest.raises(AdditiveContractError, match="Row count changed"):
            enforce_additive(frame, frame.iloc[:2])

    def test_rejects_an_unprefixed_addition(self, frame):
        produced = frame.copy()
        produced["score"] = [1, 2, 3]
        with pytest.raises(AdditiveContractError, match="must start with"):
            enforce_additive(frame, produced)
