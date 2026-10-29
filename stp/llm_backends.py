"""Chat-completion adapters returning `complete(system, user) -> str` for the GenAI layer.

  anthropic   pip install anthropic   ; environment variable ANTHROPIC_API_KEY
              (+ ANTHROPIC_WORKSPACE_ID if the key is not scoped to a workspace)
  openai      pip install openai      ; environment variable OPENAI_API_KEY
Both retry transient errors with back-off; persistent failures surface as 'llm error' (claim not understood,
never cached), so a flaky connection cannot silently corrupt results.
"""
from __future__ import annotations

import re
import time


def _json_only(text: str) -> str:
    m = re.search(r"\{.*\}", text, re.S)
    return m.group(0) if m else text


def _retry(fn, tries=4):
    for k in range(tries):
        try:
            return fn()
        except Exception:
            if k == tries - 1:
                raise
            time.sleep(2 ** k)


def anthropic_complete(model: str, max_tokens: int = 2000, json_mode: bool = True):
    import os
    state = {}

    def client():
        # created on first use, so runs that are fully answered from results/llm_cache need no key or SDK
        if "c" not in state:
            import anthropic
            ws = os.environ.get("ANTHROPIC_WORKSPACE_ID")
            state["c"] = anthropic.Anthropic(default_headers={"anthropic-workspace-id": ws} if ws else None)
        return state["c"]

    def complete(system: str, user: str) -> str:
        # recent models think before answering; a too-small budget returns NO text (stop_reason=max_tokens)
        budget = max_tokens
        for _ in range(3):
            msg = _retry(lambda: client().messages.create(model=model, max_tokens=budget,
                                                        system=system, messages=[{"role": "user", "content": user}]))
            if msg.stop_reason != "max_tokens":
                break
            budget *= 2
        else:
            raise RuntimeError(f"response truncated at {budget // 2} tokens")
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return _json_only(text) if json_mode else text.strip()
    return complete


def openai_complete(model: str, max_tokens: int = 2000, json_mode: bool = True):
    state = {}

    def client():
        if "c" not in state:
            from openai import OpenAI
            state["c"] = OpenAI()
        return state["c"]

    def complete(system: str, user: str) -> str:
        r = _retry(lambda: client().chat.completions.create(
            model=model, temperature=0, max_tokens=max_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}]))
        text = r.choices[0].message.content or ""
        return _json_only(text) if json_mode else text.strip()
    return complete


def make_complete(provider: str, model: str, json_mode: bool = True):
    if provider == "anthropic":
        return anthropic_complete(model, json_mode=json_mode)
    if provider == "openai":
        return openai_complete(model, json_mode=json_mode)
    raise ValueError(provider)
