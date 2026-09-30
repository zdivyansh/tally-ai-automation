# tally-ai

[![CI](https://github.com/zdivyansh/tally-ai-automation/actions/workflows/ci.yml/badge.svg)](https://github.com/zdivyansh/tally-ai-automation/actions/workflows/ci.yml)

Create Tally vouchers from plain-language messages (English or Hinglish), e.g.

> sold 3 ctn Crunchy 300 5/- to Sharma ji

The LLM only turns the message into structured fields. Matching names to Tally
masters, GST, round-off, numbering and XML are ordinary tested code, so the
same input always produces the same voucher.

> **Status:** sales invoices work end to end from the CLI. Purchase, receipt
> and payment vouchers are planned. The business rules (rate, discount, GST,
> round-off, numbering, confirmation) are in [docs/sales-rules.md](docs/sales-rules.md).

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Tally (Prime or ERP 9) with the
XML/HTTP server enabled (F1 > Settings > Connectivity, port 9000).

```sh
uv sync                    # creates .venv and installs everything
cp .env.example .env       # then edit TALLY_HOST, LLM settings, ...
uv run tally-ai doctor     # checks Tally, master data, sales ledgers and the LLM
uv run tally-ai chat       # enter sales in plain language
```

### Example

```
you> sold 3 ctn Crunchy 300 5/- to sharma

Rate for Crunchy (300) 5/- (3 Ctn)?
  - last to Sharma Traders: ₹1,300.00/Ctn on 10-Sep-2026
  - last to anyone: ₹1,310.63/Ctn on 28-Sep-2026 (Gupta Store (Station))
Reply with a rate, 'same' (last to anyone), 'customer' (last to this customer).

you> same

Sales invoice ABC/26-27/0008  |  30-Sep-2026
Customer: Sharma Traders (Jharkhand)

1. Crunchy (300) 5/-
   3 Ctn x ₹1,310.63 less 12% = ₹3,460.06
   rate: item's last rate (you chose); discount: customer's last (10-Sep-2026)

   Taxable     ₹3,460.06
   CGST 2.5%   ₹86.50
   SGST 2.5%   ₹86.50
   Round off   -₹0.06
   Total       ₹3,633.00

Reply 'yes' to post, 'no' to cancel, or type a correction (e.g. 'rate 1300', 'qty 5').

you> yes

Posted sales invoice ABC/26-27/0008 for Sharma Traders, total ₹3,633.00.
```

Ambiguous names are asked as numbered choices; `cancel` at any question stops
without posting.

### Audit log

Every conversation is recorded in a local SQLite file (`AUDIT_DB_PATH`,
default `data/audit.db`, gitignored because it holds customer data):

- `conversations`: message, channel, user, outcome, final draft, voucher number and total
- `events`: every agent message and user reply, in order
- `postings`: every attempt to post, with the exact XML sent and Tally's reply

Open it with any SQLite viewer, e.g.
`sqlite3 data/audit.db "select started_at, party, voucher_number, total, status from conversations"`.

### Purchase invoices (PepsiCo)

Save PepsiCo invoice PDFs in a folder (`PURCHASE_INVOICE_DIR` in `.env`) and run

```sh
uv run tally-ai purchase learn   # once: learn item mapping from bills already in Tally
uv run tally-ai purchase watch   # confirm each new invoice; posts after your 'yes'
```

Invoices are read exactly (digital PDFs, no LLM), must reconcile to the
printed totals, and are never posted twice. The PepsiCo -> Tally item mapping
is an Excel-friendly `pepsico_mapping.csv` in the same folder. See
[docs/purchase-invoices.md](docs/purchase-invoices.md).

### Evaluation

```sh
uv run tally-ai eval generate --count 300   # realistic messages from your past invoices
uv run tally-ai eval run                    # extraction + matching accuracy
```

The key number is "picked WRONG without asking", which must stay at 0. See
[docs/evaluation.md](docs/evaluation.md).

### LLM providers

Set `LLM_PROVIDER` in `.env`:

| Provider | Setting | Notes |
|---|---|---|
| `ollama` (default) | `OLLAMA_MODEL=gemma4:e4b` | Local, free. `ollama pull gemma4:e4b` |
| `gemini` | `GEMINI_API_KEY`, `GEMINI_MODEL` | Google AI Studio key |
| `openrouter` | `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` | Any OpenRouter model |

## Project layout

```
src/tally_ai/
  config.py          settings from env / .env
  agents/sales/      the sales agent (LangGraph)
    extraction.py    LLM prompt + guards: message -> structured fields
    draft.py         what is known, what to ask next, applying answers
    compose.py       draft -> computed invoice with explanations
    graph.py         extract -> resolve <-> ask -> build -> confirm -> post
    render.py        user-facing text (channel-agnostic)
  accounting/        dates, numbering, sales history, invoice maths
  evaluation/        eval case format, generator, runner
  purchase/          PepsiCo invoice parser, mapping CSV, purchase voucher, folder watcher
  audit.py           SQLite audit log
  masters/           cached master data, fuzzy matcher
  tally/             Tally access, no LLM code
    client.py        HTTP transport; turns Tally's in-band errors into exceptions
    xml_builder.py   voucher / export / delete XML (ElementTree, no templates)
    models.py        Voucher, LedgerLine, InventoryLine, ImportResult (validated)
    masters.py       Ledger, StockItem, GST rate history + resolution
    queries.py       typed read-only queries
  llm/factory.py     LangChain chat model for the configured provider
  interfaces/cli.py  `tally-ai` command
tests/
  unit/              fast tests, no Tally or LLM needed
  fixtures/          synthetic Tally responses
  live/              opt-in tests against a real Tally server
  eval/              LLM extraction benchmark (real cases in a gitignored file)
```

## Development

```sh
uv run pytest                          # unit tests
TALLY_LIVE=1 uv run pytest -m live     # also run against the Tally in .env
uv run tally-ai eval run --no-llm       # quick matching regression after matcher changes
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

CI (GitHub Actions) runs lint, format check, mypy and the unit tests on every
push to `master` and on pull requests. Live Tally tests and LLM evaluation run
only locally.

Live tests create vouchers numbered `TEST/CLAUDE/...` and delete them again.
Never point them at a company you cannot restore from backup.

## Notes on Tally behaviour

Things this code handles that are easy to get wrong:

- Tally reports import failures inside an HTTP 200 reply (`LINEERROR`,
  `ERRORS`, `EXCEPTIONS`). The client raises `TallyImportError`.
- Replies can contain control characters that are illegal in XML, both raw
  and as `&#4;` entities. They are stripped before parsing.
- Voucher exports only cover the current period unless a date range is given.
- GST rates are dated (e.g. 12% until 21-Sep-2025, then 5%) and are often
  inherited from the stock group ("As per Company/Stock Group").
- Educational mode only accepts voucher dates on the 1st, 2nd and 31st.

## License

Apache-2.0
