# tally-ai

Create Tally vouchers from plain-language messages (English or Hinglish), e.g.

> sold 3 ctn Crunchy 300 5/- to Sharma ji

The LLM only turns the message into structured fields. Matching names to Tally
masters, GST, round-off, numbering and XML are ordinary tested code, so the
same input always produces the same voucher.

> **Status:** phase 1 (foundation) is done: Tally client, typed models, XML
> builder, LLM provider factory, `doctor` CLI. The natural-language agent
> arrives in phase 2; the old prototype in `app/` is kept until then.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Tally (Prime or ERP 9) with the
XML/HTTP server enabled (F1 > Settings > Connectivity, port 9000).

```sh
uv sync                    # creates .venv and installs everything
cp .env.example .env       # then edit TALLY_HOST, LLM settings, ...
uv run tally-ai doctor     # checks Tally, master data and the LLM
```

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
  eval/              LLM extraction benchmarks
```

## Development

```sh
uv run pytest                          # unit tests
TALLY_LIVE=1 uv run pytest -m live     # also run against the Tally in .env
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

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
