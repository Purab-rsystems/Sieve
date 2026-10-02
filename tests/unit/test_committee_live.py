"""Live: the real models. Opt-in (SIEVE_LIVE=1), needs the model files in
SIEVE_MODEL_DIR and about 6 GB of free memory; one model is loaded at a time.

Re-asks recorded benchmark questions and checks the models give the same
answer, so the recorded accuracy numbers reproduce from the models themselves.
"""

from __future__ import annotations

import os
import random
from pathlib import Path

import pytest

from sieve.committee.decide import AnswerStore, Ask, ModelRunner

pytestmark = pytest.mark.skipif(os.getenv("SIEVE_LIVE") != "1", reason="set SIEVE_LIVE=1 to run the models")

ROOT = Path(__file__).resolve().parents[2]
ANSWERS = ROOT / "benchmarks" / "committee" / "answers.json"
STATE = Path(os.getenv("SIEVE_STATE_DIR", ROOT / "benchmarks" / "committee" / ".model_state"))


@pytest.mark.parametrize("which", ["judge", "checker"])
def test_models_reproduce_recorded_answers(which, tmp_path):
    recorded = AnswerStore(ANSWERS)
    rows = [r for r in recorded.data.values() if r["model"].startswith("qwen" if which == "judge" else "llama")]
    if not rows:
        pytest.skip("no recorded answers")
    random.Random(0).shuffle(rows)
    fresh = AnswerStore(tmp_path / "fresh.json")
    runner = ModelRunner(fresh, STATE, log=lambda *_: None)
    try:
        asks = [Ask(which, r["campaign"], r["role"], tuple(r["options"])) for r in rows[:2]]
        runner.answer(asks)
        for r, key in zip(rows[:2], fresh.data):
            again = fresh.data[key]["probs"]
            top = max(r["probs"], key=r["probs"].get)
            assert max(again, key=again.get) == top
            for f, p in r["probs"].items():
                assert abs(again[f] - p) < 0.02, (f, p, again[f])
    finally:
        runner.close()
