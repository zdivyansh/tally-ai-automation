"""
Quick extraction benchmark for local Ollama models.

Sends Hinglish/shorthand voucher messages and checks the structured JSON
the model returns against expected values.

Cases come from cases.local.json if present (gitignored, real messages),
else cases.example.json.

Usage:
  uv run python tests/eval/ollama_model_bench.py gemma4:e4b qwen3:8b
"""

import json
import sys
import time
from pathlib import Path

import requests

OLLAMA = "http://localhost:11434/api/chat"

SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["sales", "purchase", "receipt", "payment", "query", "unknown"]},
        "party": {"type": ["string", "null"]},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "qty": {"type": "number"},
                    "unit": {"type": ["string", "null"]},
                    "rate": {"type": ["number", "null"]},
                },
                "required": ["name", "qty", "unit", "rate"],
            },
        },
        "amount": {"type": ["number", "null"]},
        "date_text": {"type": ["string", "null"]},
    },
    "required": ["intent", "party", "items", "amount", "date_text"],
}

SYSTEM = (
    "You extract accounting voucher details from short Indian business messages "
    "(English, Hindi or Hinglish). Copy party and item names exactly as the user wrote them; "
    "do not correct or expand them. Use null when a value is not stated. "
    "intent: sales = sold/becha/diya; purchase = bought/kharida/liya from supplier; "
    "receipt = money received/mila/aaya; payment = money paid/diya to someone; query = question."
)

HERE = Path(__file__).parent
# Real messages (with real customer names) live in cases.local.json, which is gitignored
CASES_FILE = (
    HERE / "cases.local.json" if (HERE / "cases.local.json").exists() else HERE / "cases.example.json"
)


def load_cases() -> list[tuple[str, dict]]:
    cases = []
    for case in json.loads(CASES_FILE.read_text(encoding="utf-8")):
        expected = dict(case["expected"])
        if "items" in expected:
            expected["items"] = [(i["name"], i["qty"], i["unit"]) for i in expected["items"]]
        cases.append((case["message"], expected))
    return cases


def norm(s):
    return " ".join(str(s or "").lower().replace(".", "").split())


def score(out, exp):
    checks = []
    checks.append(out.get("intent") == exp["intent"])
    checks.append(
        norm(exp["party"]) in norm(out.get("party")) or norm(out.get("party")) in norm(exp["party"])
        if out.get("party")
        else False
    )
    if "items" in exp:
        got = out.get("items") or []
        checks.append(len(got) == len(exp["items"]))
        for (n, q, u), g in zip(exp["items"], got, strict=False):
            checks.append(
                norm(n).replace(" ", "") in norm(g.get("name")).replace(" ", "")
                or norm(g.get("name")).replace(" ", "") in norm(n).replace(" ", "")
            )
            checks.append(float(g.get("qty") or 0) == q)
            checks.append(norm(g.get("unit")) == u)
    if "amount" in exp:
        checks.append(float(out.get("amount") or 0) == exp["amount"])
    if "date_text" in exp:
        checks.append(norm(exp["date_text"]) in norm(out.get("date_text")))
    return sum(checks), len(checks)


def run(model):
    total_ok = total_n = 0
    times = []
    for msg, exp in load_cases():
        body = {
            "model": model,
            "stream": False,
            "format": SCHEMA,
            "think": False,
            "options": {"temperature": 0},
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": msg}],
        }
        t0 = time.time()
        try:
            r = requests.post(OLLAMA, json=body, timeout=300)
            r.raise_for_status()
            out = json.loads(r.json()["message"]["content"])
        except Exception as e:
            out = {"error": str(e)[:120]}
        dt = time.time() - t0
        times.append(dt)
        ok, n = score(out, exp) if "error" not in out else (0, 1)
        total_ok += ok
        total_n += n
        print(f"  [{ok}/{n}] {dt:5.1f}s  {msg}\n         -> {json.dumps(out, ensure_ascii=False)}")
    warm = times[1:] or times
    print(
        f"== {model}: {total_ok}/{total_n} checks ({100 * total_ok / total_n:.0f}%), "
        f"first call {times[0]:.1f}s, avg warm {sum(warm) / len(warm):.1f}s\n"
    )


if __name__ == "__main__":
    for m in sys.argv[1:] or ["gemma4:latest"]:
        print(f"### {m}")
        run(m)
