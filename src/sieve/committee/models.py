"""Local models for the committee step: Qwen2.5-7B (judge), Llama-3.1-8B (checker).

Both run on CPU through llama.cpp. Nothing is generated: every answer is read
from the model's own next-token probabilities, so no title or function can be
invented and no answer can be malformed.

    judge    answers a lettered multiple-choice question; the probability of
             each option letter is read, over four option orders, to cancel
             the pull towards the first letter.
    checker  answers with the function's name, scored token by token (cloze
             prompting), over two option orders. Llama binds answers to option
             letters poorly; smaller models do better scored on the answer text
             (Robinson & Wingate, ICLR 2023).

The long shared part of every prompt (role definitions and the role reference
sheet) is evaluated once per model and its state saved to disk, so each
campaign only costs its own text and questions.

Only one model is in memory at a time (the two do not fit together in 7 GB).
"""

from __future__ import annotations

import gc
import hashlib
import os
import pickle
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

def _default_model_dir() -> Path:
    try:
        return Path.home() / "llm"
    except RuntimeError:                 # no home directory known (stripped environment)
        return Path("llm").resolve()


MODEL_DIR = Path(os.getenv("SIEVE_MODEL_DIR") or _default_model_dir())


@dataclass(frozen=True)
class ModelSpec:
    name: str
    path: Path
    family: str                  # "qwen" | "llama"
    style: str                   # "letters" | "cloze"
    orders: int


JUDGE = ModelSpec(
    "qwen2.5-7b-instruct-q4_k_m",
    Path(os.getenv("SIEVE_JUDGE_MODEL", MODEL_DIR / "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf")),
    "qwen", "letters", 4)
CHECKER = ModelSpec(
    "llama-3.1-8b-instruct-q4_k_m",
    Path(os.getenv("SIEVE_CHECKER_MODEL", MODEL_DIR / "llama-3.1-8b-instruct-q4_k_m.gguf")),
    "llama", "cloze", 2)

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def chat_head(family: str, system: str) -> str:
    if family == "qwen":
        return f"<|im_start|>system\n{system}<|im_end|>\n<|im_start|>user\n"
    return (f"<|start_header_id|>system<|end_header_id|>\n\n{system}<|eot_id|>"
            f"<|start_header_id|>user<|end_header_id|>\n\n")


def chat_tail(family: str) -> str:
    if family == "qwen":
        return "<|im_end|>\n<|im_start|>assistant\n"
    return "<|eot_id|><|start_header_id|>assistant<|end_header_id|>\n\n"


def option_orders(options: Sequence[str], n: int) -> list[list[str]]:
    """n orders of the options: forward, reversed, and (for 4) both halves
    rotated, so every order starts with a different option."""
    base = list(options)
    orders = [base, base[::-1]]
    if n >= 4:
        k = len(base) // 2
        rot = base[k:] + base[:k]
        orders += [rot, rot[::-1]]
    return orders[:n]


def _log_softmax(z: np.ndarray) -> np.ndarray:
    m = z.max()
    return z - (m + np.log(np.exp(z - m).sum()))


class LocalModel:
    """One llama.cpp model with a cached shared prefix."""

    def __init__(self, spec: ModelSpec, n_ctx: int = 8192, state_dir: Path | None = None,
                 threads: int | None = None) -> None:
        from llama_cpp import Llama          # optional dependency: sieve[committee]

        if not spec.path.exists():
            raise FileNotFoundError(f"{spec.name}: model file not found at {spec.path}")
        self.spec = spec
        self.state_dir = Path(state_dir) if state_dir else None
        t0 = time.time()
        self.llm = Llama(model_path=str(spec.path), n_ctx=n_ctx, n_batch=256,
                         n_threads=threads or os.cpu_count(), flash_attn=True,
                         type_k=8, type_v=8, logits_all=False, verbose=False)
        self.load_seconds = time.time() - t0
        self.n_vocab = self.llm.n_vocab()
        self._prefix_len = 0
        self._campaign_key: str | None = None
        self._campaign_len = 0
        self.calls = 0
        self.seconds = 0.0

    # ------------------------------------------------------------ tokens
    def tok(self, text: str) -> list[int]:
        return self.llm.tokenize(text.encode("utf-8"), add_bos=False, special=True)

    def _eval(self, tokens: list[int]) -> np.ndarray:
        self.llm.eval(tokens)
        return np.ctypeslib.as_array(self.llm._ctx.get_logits(), shape=(self.n_vocab,)).copy()

    # ------------------------------------------------------------ prefix
    def set_prefix(self, system: str, shared_text: str) -> None:
        """Evaluate the shared opening once (or load it from disk)."""
        text = chat_head(self.spec.family, system) + shared_text
        tokens = ([self.llm.token_bos()] if self.spec.family == "llama" else []) + self.tok(text)
        key = hashlib.sha1((self.spec.name + "\x00" + text).encode()).hexdigest()[:16]
        path = self.state_dir / f"{self.spec.name}-{key}.state" if self.state_dir else None
        self.llm.reset()
        if path and path.exists():
            with open(path, "rb") as fh:
                self.llm.load_state(pickle.load(fh))
        else:
            t0 = time.time()
            self.llm.eval(tokens)
            self.prefix_seconds = time.time() - t0
            if path:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                with open(tmp, "wb") as fh:
                    pickle.dump(self.llm.save_state(), fh)
                tmp.replace(path)
        self._prefix_len = len(tokens)
        self._campaign_key = None

    def _set_campaign(self, campaign_text: str) -> None:
        if self._campaign_key == campaign_text:
            self.llm.n_tokens = self._campaign_len
            return
        self.llm.n_tokens = self._prefix_len
        self._eval(self.tok(campaign_text))
        self._campaign_key = campaign_text
        self._campaign_len = self.llm.n_tokens

    # ------------------------------------------------------------ answers
    def letter_probs(self, campaign_text: str, question: str, options: Sequence[str],
                     options_block) -> dict[str, float]:
        """Probability of each option, read from the option letters, averaged over orders."""
        t0 = time.time()
        total = {o: 0.0 for o in options}
        orders = option_orders(options, self.spec.orders)
        for order in orders:
            self._set_campaign(campaign_text)
            letters = LETTERS[:len(order)]
            text = question + "\n" + options_block(order, letters) + "\nAnswer with one letter." + chat_tail(self.spec.family)
            z = self._eval(self.tok(text))
            ids = [self.tok(l)[0] for l in letters]
            lp = _log_softmax(np.array([z[i] for i in ids]))
            for o, p in zip(order, np.exp(lp)):
                total[o] += float(p) / len(orders)
        self.calls += len(orders)
        self.seconds += time.time() - t0
        return total

    def cloze_probs(self, campaign_text: str, question: str, options: Sequence[str],
                    options_block) -> dict[str, float]:
        """Probability of each option, scored on the option's own words, averaged over orders."""
        t0 = time.time()
        total = {o: 0.0 for o in options}
        orders = option_orders(options, self.spec.orders)
        for order in orders:
            self._set_campaign(campaign_text)
            text = question + "\n" + options_block(order, None) + "\nAnswer with the function's name only." + chat_tail(self.spec.family)
            z0 = self._eval(self.tok(text))
            base = self.llm.n_tokens
            scores = []
            for o in order:
                ids = self.tok(o)
                lp = float(_log_softmax(z0)[ids[0]])
                self.llm.n_tokens = base
                for k in range(1, len(ids)):
                    z = self._eval([ids[k - 1]])
                    lp += float(_log_softmax(z)[ids[k]])
                scores.append(lp)
            p = np.exp(_log_softmax(np.array(scores)))
            for o, v in zip(order, p):
                total[o] += float(v) / len(orders)
        self.calls += len(orders)
        self.seconds += time.time() - t0
        return total

    def close(self) -> None:
        try:
            self.llm.close()
        except Exception:
            pass
        self.llm = None
        gc.collect()
