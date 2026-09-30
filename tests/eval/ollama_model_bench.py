"""Extraction benchmark: runs the production extractor (prompt + guards) on example messages.

Cases come from cases.local.json if present (gitignored, real messages),
else cases.example.json.

Usage:
  uv run python tests/eval/ollama_model_bench.py                 # model from .env
  uv run python tests/eval/ollama_model_bench.py gemma4:e4b qwen3:8b
"""

import json
import sys
import time
from pathlib import Path
from typing import Any

from tally_ai.agents.sales.extraction import Extraction, LLMExtractor
from tally_ai.config import LLMProvider, Settings
from tally_ai.llm import create_chat_model

HERE = Path(__file__).parent
LOCAL_CASES = HERE / "cases.local.json"
CASES_FILE = LOCAL_CASES if LOCAL_CASES.exists() else HERE / "cases.example.json"


def norm(value: object) -> str:
    return "".join(str(value or "").lower().replace(".", "").split())


def close(a: object, b: object) -> bool:
    x, y = norm(a), norm(b)
    return bool(x and y) and (x in y or y in x)


def score(out: Extraction, expected: dict[str, Any]) -> tuple[int, int, list[str]]:
    checks: list[tuple[str, bool]] = [
        ("intent", out.intent == expected["intent"]),
        ("party", close(out.party, expected.get("party"))),
    ]
    if "items" in expected:
        checks.append(("item count", len(out.items) == len(expected["items"])))
        for exp, got in zip(expected["items"], out.items, strict=False):
            checks.append((f"name {exp['name']!r}", close(got.name, exp["name"])))
            checks.append((f"qty {exp['qty']}", got.quantity == exp["qty"]))
            checks.append((f"unit {exp['unit']}", norm(got.unit) == norm(exp["unit"])))
            checks.append((f"rate {exp.get('rate')}", got.rate == exp.get("rate")))
            checks.append((f"discount {exp.get('discount')}", got.discount_pct == exp.get("discount")))
    if "date_text" in expected:
        checks.append(("date", norm(expected["date_text"]) == norm(out.date_text)))
    failed = [name for name, ok in checks if not ok]
    return len(checks) - len(failed), len(checks), failed


def run(settings: Settings) -> None:
    extractor = LLMExtractor(create_chat_model(settings))
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    total_ok = total = 0
    times = []
    for case in cases:
        start = time.perf_counter()
        try:
            out = extractor.extract(case["message"])
            ok, n, failed = score(out, case["expected"])
            detail = out.model_dump_json(exclude_none=True)
        except Exception as e:  # report and continue
            ok, n, failed, detail = 0, 1, ["error"], f"{type(e).__name__}: {e}"
        times.append(time.perf_counter() - start)
        total_ok += ok
        total += n
        flag = "OK  " if not failed else "MISS"
        print(f"  {flag} [{ok}/{n}] {times[-1]:5.1f}s  {case['message']}")
        if failed:
            print(f"         failed: {', '.join(failed)}\n         got: {detail}")
    warm = times[1:] or times
    model = settings.ollama_model if settings.llm_provider is LLMProvider.OLLAMA else ""
    print(
        f"== {settings.llm_provider}:{model} {total_ok}/{total} checks "
        f"({100 * total_ok / total:.0f}%), avg {sum(warm) / len(warm):.1f}s\n"
    )


if __name__ == "__main__":
    base = Settings()
    models = sys.argv[1:]
    print(f"cases: {CASES_FILE.name}")
    for model in models or [None]:
        run(base.model_copy(update={"ollama_model": model}) if model else base)
