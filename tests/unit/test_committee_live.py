"""Live: the real models through Ollama. Opt-in (SIEVE_LIVE=1); needs Ollama
running with qwen2.5:7b and llama3.1:8b pulled.

Asks one real committee question of each model and checks the answer has the
shape Sieve relies on: written reasoning, then a probability for every option
read after it. Then asks it again: at temperature 0 the answer must repeat.
"""

from __future__ import annotations

import os

import pytest

from sieve.committee.campaign import campaign_text
from sieve.committee.decide import SYSTEM, campaign_block, question_text, shared_text
from sieve.committee.models import CHECKER, JUDGE, OllamaModel, check_ollama
from sieve.committee.reference import functions

pytestmark = pytest.mark.skipif(os.getenv("SIEVE_LIVE") != "1", reason="set SIEVE_LIVE=1 to run the models")


@pytest.mark.parametrize("spec", [JUDGE, CHECKER], ids=["qwen-judge", "llama-checker"])
def test_model_reasons_then_answers_and_repeats(spec):
    assert check_ollama([spec]) == []
    opts = tuple(sorted(functions()))
    user = (shared_text() + campaign_block(campaign_text({"offering": {"offering": "Cloud HR and payroll software"}}))
            + question_text("Champion", opts))
    other = OllamaModel(CHECKER if spec is JUDGE else JUDGE)
    m = OllamaModel(spec, other=other)
    a = m.ask(SYSTEM, user, opts)
    assert len(a["reasoning"]) > 40
    assert set(a["probs"]) == set(opts) and abs(sum(a["probs"].values()) - 1) < 1e-6
    b = m.ask(SYSTEM, user, opts)
    assert max(a["probs"], key=a["probs"].get) == max(b["probs"], key=b["probs"].get)
