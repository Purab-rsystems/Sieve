"""The committee models, run by Ollama: Qwen2.5-7B (judge), Llama-3.1-8B (checker).

Ollama is a separate Windows/macOS/Linux app that runs the models; Sieve
talks to it over http://localhost:11434 with the standard library only, so
Sieve itself is plain Python (3.14 included) with nothing to compile.

    ollama pull qwen2.5:7b
    ollama pull llama3.1:8b

How a model answers a question (reason first, then answer)
------------------------------------------------------------
1. The model writes its reasoning, at temperature 0, until it starts its
   answer line ("Answer:").
2. Sieve then appends "Answer:" to that reasoning and reads the model's
   probability for the first word of every option at that point (Ollama's
   logprobs). The answer is read *after* the model's own reasoning, so the
   reasoning shown is what the answer was produced from.

The options are function names, answered by name: smaller models tie answers
to option letters poorly (Robinson & Wingate, ICLR 2023).
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Sequence

OLLAMA_URL = os.getenv("SIEVE_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
NUM_CTX = int(os.getenv("SIEVE_NUM_CTX", "6144"))      # the prompt is ~5,300 tokens
REASON_TOKENS = int(os.getenv("SIEVE_REASON_TOKENS", "220"))
TOP_LOGPROBS = 20                          # the most Ollama returns per token


@dataclass(frozen=True)
class ModelSpec:
    name: str        # shown in results and used in the answer store key
    tag: str         # Ollama model tag
    family: str      # "qwen" | "llama": chat template


JUDGE = ModelSpec("qwen2.5-7b-instruct", os.getenv("SIEVE_JUDGE_MODEL", "qwen2.5:7b"), "qwen")
CHECKER = ModelSpec("llama-3.1-8b-instruct", os.getenv("SIEVE_CHECKER_MODEL", "llama3.1:8b"), "llama")


class OllamaError(RuntimeError):
    pass


def chat_prompt(family: str, system: str, user: str) -> str:
    """The model's own chat template, written out so the prompt is exact."""
    if family == "qwen":
        return (f"<|im_start|>system\n{system}<|im_end|>\n<|im_start|>user\n{user}<|im_end|>\n"
                f"<|im_start|>assistant\n")
    return (f"<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n\n{system}<|eot_id|>"
            f"<|start_header_id|>user<|end_header_id|>\n\n{user}<|eot_id|>"
            f"<|start_header_id|>assistant<|end_header_id|>\n\n")


def _post(path: str, body: dict, timeout: float = 1800) -> dict:
    req = urllib.request.Request(OLLAMA_URL + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise OllamaError(f"Ollama {path}: {e.code} {e.read().decode(errors='ignore')[:300]}") from e
    except urllib.error.URLError as e:
        raise OllamaError(f"Ollama is not reachable at {OLLAMA_URL}. Start the Ollama app "
                          f"(or run `ollama serve`). ({e.reason})") from e


def check_ollama(specs: Sequence[ModelSpec] = (JUDGE, CHECKER)) -> list[str]:
    """Problems that stop the models from running ([] when all is well)."""
    try:
        req = urllib.request.Request(OLLAMA_URL + "/api/tags")
        with urllib.request.urlopen(req, timeout=5) as r:
            have = {m["name"] for m in json.loads(r.read()).get("models", [])}
    except Exception as e:  # noqa: BLE001
        return [f"Ollama is not running at {OLLAMA_URL}: start the Ollama app. ({e})"]
    problems = []
    for s in specs:
        if s.tag not in have and f"{s.tag}:latest" not in have:
            problems.append(f"Model {s.tag} is not pulled: run `ollama pull {s.tag}`")
    return problems


def option_probs(top: list[dict], options: Sequence[str]) -> dict[str, float]:
    """Spread the next-token probabilities over the options whose name the
    token starts. A token shared by several names (e.g. "P") is split evenly
    between them; tokens that start no option are dropped and the rest
    renormalised."""
    mass = {o: 0.0 for o in options}
    for t in top:
        tok = t["token"].strip().lower()
        if not tok:
            continue
        hits = [o for o in options if o.lower().startswith(tok)]
        if not hits:
            continue
        p = math.exp(t["logprob"])
        for o in hits:
            mass[o] += p / len(hits)
    total = sum(mass.values())
    if total <= 0:
        return {o: 1.0 / len(options) for o in options}
    return {o: v / total for o, v in mass.items()}


class OllamaModel:
    """One model behind Ollama. Same shared prompt prefix for every question,
    so Ollama's prompt cache only evaluates each campaign's own part."""

    def __init__(self, spec: ModelSpec, other: "OllamaModel | None" = None) -> None:
        self.spec = spec
        self.other = other              # the model to free if memory runs out
        self.calls = 0
        self.seconds = 0.0

    def unload(self) -> None:
        """Free this model's memory in Ollama."""
        try:
            _post("/api/generate", {"model": self.spec.tag, "keep_alive": 0}, timeout=60)
        except OllamaError:
            pass

    def _generate(self, prompt: str, num_predict: int, logprobs: bool, stop: list[str] | None = None) -> dict:
        body = {"model": self.spec.tag, "prompt": prompt, "raw": True, "stream": False,
                "keep_alive": "30m",
                "options": {"temperature": 0, "seed": 0, "num_ctx": NUM_CTX, "num_predict": num_predict,
                            "use_mmap": True}}
        if stop:
            body["options"]["stop"] = stop
        if logprobs:
            body["logprobs"] = True
            body["top_logprobs"] = TOP_LOGPROBS
        try:
            out = _post("/api/generate", body)
        except OllamaError as e:
            # Not enough memory for both models at once: free the other one and retry.
            if self.other is None or not re.search(r"terminated|killed|memory|resources|timed out", str(e), re.I):
                raise
            self.other.unload()
            out = _post("/api/generate", body)
        used = int(out.get("prompt_eval_count") or 0) + int(out.get("eval_count") or 0)
        if used >= NUM_CTX:
            # Ollama drops the start of a prompt that does not fit: never accept that silently.
            raise OllamaError(f"prompt and answer need {used} tokens but the context is {NUM_CTX}: "
                              f"set SIEVE_NUM_CTX higher")
        return out

    def ask(self, system: str, user: str, options: Sequence[str]) -> dict:
        """Reasoning, then the probability of each option read after it."""
        t0 = time.time()
        prompt = chat_prompt(self.spec.family, system, user)
        first = self._generate(prompt, REASON_TOKENS, logprobs=False, stop=["Answer:", "ANSWER:"])
        reasoning = re.split(r"\n?\s*(?:\*\*)?Answer\s*:", first.get("response", ""), maxsplit=1)[0].strip()
        second = self._generate(prompt + reasoning + "\nAnswer:", 1, logprobs=True)
        lp = second.get("logprobs") or []
        if not lp:
            raise OllamaError("Ollama returned no logprobs: update Ollama to 0.12 or newer.")
        top = lp[0].get("top_logprobs") or [{"token": lp[0]["token"], "logprob": lp[0]["logprob"]}]
        probs = option_probs(top, options)
        self.calls += 1
        self.seconds += time.time() - t0
        return {"probs": probs, "reasoning": reasoning,
                "answer_token": lp[0].get("token", "").strip()}

    def write(self, system: str, user: str, max_tokens: int = 160) -> str:
        """Plain text from the model (temperature 0), e.g. a role description."""
        out = self._generate(chat_prompt(self.spec.family, system, user), max_tokens, logprobs=False)
        return out.get("response", "").strip()
