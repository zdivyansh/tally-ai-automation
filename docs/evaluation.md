# Evaluation

Measures two things on realistic messages, without posting anything:

1. **Extraction**: did the LLM read the message correctly (customer text,
   item texts, quantity, unit, and rate / discount / date only when stated)?
2. **Matching**: did the agent find the right Tally customer and items, or
   correctly ask with the right one among the choices?

The number that matters most is **"picked WRONG without asking"**: it must
stay at 0. Asking is fine (the user picks from a list); guessing wrong is not.

## Commands

```sh
uv run tally-ai eval generate --count 300            # cases from past invoices in Tally
uv run tally-ai eval run                             # local hand-written + generated cases
uv run tally-ai eval run --no-llm                    # matching only, seconds (after matcher changes)
uv run tally-ai eval run --model qwen3:8b --limit 50 # compare another Ollama model
uv run tally-ai eval run tests/eval/cases.example.json --no-tally   # extraction only, no Tally
uv run tally-ai eval run --fail-on-wrong             # exit code 1 if anything was picked wrong
```

`run` without file arguments uses `tests/eval/cases.local.json` and
`tests/eval/generated.local.json` if they exist. Both contain real customer
names and are gitignored; only `cases.example.json` (made-up names) is committed.

## Outcomes

For every expected customer and item:

| Outcome | Meaning | OK? |
|---|---|---|
| picked correctly | resolved without a question, correctly | yes |
| asked, correct one offered | the user gets a numbered list containing the right one | yes |
| asked, correct one NOT offered | the user has to retype the name | no |
| picked WRONG without asking | a wrong master was chosen silently | **never** |
| not extracted | the LLM did not return the customer / item | no |

## Case format

A JSON list. Every field except `message` is optional.

```json
[
  {
    "message": "Gupta store ko 5 ctn masala puff 10/- @850 less 12% bheja kal",
    "expected": {
      "intent": "sales",
      "party": "Gupta store",
      "items": [{"name": "masala puff 10/-", "qty": 5, "unit": "ctn", "rate": 850, "discount": 12}],
      "date_text": "kal"
    },
    "expected_match": {
      "party": "Gupta Store (Main Road)",
      "items": ["Masala Puff (240) 10/-"]
    }
  }
]
```

- `expected`: what the LLM should read. `party` and item `name` are the text
  as written; comparison ignores case, spaces and punctuation. Omitted `rate`
  / `discount` mean "must not be invented".
- `expected_match`: exact Tally ledger / stock item names, in message order.
- `texts` (generated cases): the exact texts, used by `--no-llm`.

To add a real message you received, append it to
`tests/eval/cases.local.json` with the Tally names you expect.

## Generated cases

`eval generate` picks random past sales invoices (current and previous
financial year, customers under Sundry Debtors, items that still exist) and
writes 1-2 item messages the way people type them: lower case, brackets
dropped, `5/-` sometimes as `5rs`, in English and Hinglish templates. The
seed makes the set reproducible (`--seed`).
