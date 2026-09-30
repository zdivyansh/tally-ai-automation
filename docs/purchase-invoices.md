# Purchase invoices (PepsiCo)

The agent watches a folder for PepsiCo tax invoice PDFs and turns each one
into a Tally purchase voucher after you confirm it. No LLM is involved: the
PDFs are digital SAP invoices, so they are read exactly.

## Setup

In `.env` (full path, anywhere on the PC):

```
PURCHASE_INVOICE_DIR=D:\PepsicoInvoices
```

```sh
uv run tally-ai purchase learn    # once: pre-fill the mapping from bills already in Tally
uv run tally-ai purchase watch    # keep running; confirm each invoice in this window
uv run tally-ai purchase check D:\PepsicoInvoices\5464064806.PDF   # dry run, posts nothing
```

## What happens with each PDF

1. **Read and reconcile.** Every line must add up (taxable + GST = line
   total, GST = rate x taxable) and the lines must sum exactly to the printed
   grand totals. Anything that does not add up is reported and not posted.
   Invoices with TCS are refused (not handled yet).
2. **Supplier** is found by GSTIN among Sundry Creditors (JH and WB ledgers).
3. **Duplicate check.** If Tally already has a purchase voucher whose
   reference is this invoice number, it is not posted again.
4. **Mapping.** Each line is mapped to a Tally stock item with the rules in
   the mapping file. For a product without a rule you pick the Tally item
   (suggestions: items with the same pack and MRP first) and whether the rule
   covers all flavours of the family or only this one.
5. **Confirm.** The full entry is shown. `yes` posts it; `no` skips it until
   the watcher is started again; `ignore` never shows it again.

Files stay in the folder; what was done with each is recorded in the audit
database (`purchase_invoices` table), and every posting attempt with the exact
XML in `postings`.

## The voucher

| Field | Value |
|---|---|
| Date, reference date | invoice date |
| Reference | PepsiCo invoice number |
| Voucher number | automatic (Tally) |
| Party | supplier ledger found by GSTIN, credited with the invoice total |
| Lines | one per invoice line (not merged); quantity = invoice quantity x `ctn_per_case`; amount = the invoice's taxable value exactly; rate = amount / quantity |
| Tax | CGST + SGST or IGST exactly as printed on the invoice |
| Stock ledger / godown | `PURCHASE_LEDGER` (Purchase), Main Location / Primary Batch |
| Duplicate safety | REMOTEID `tally-ai-purchase-<GSTIN>-<invoice no>` |

## The mapping file

`pepsico_mapping.csv` in the invoice folder; open it in Excel. Changes are
picked up without restarting the watcher.

| Column | Meaning |
|---|---|
| keywords | words that must appear in the invoice description, e.g. `Lays` or `Lays Mini Stix` |
| pack | pieces per case on the invoice, e.g. 180 |
| mrp | MRP per piece, e.g. 10 |
| tally_item | exact Tally stock item name |
| ctn_per_case | Tally units per invoice case (1 unless you change it, e.g. 2) |
| source | `you` or `learned from bill ...` |
| updated | date written |

Rules match on pack + MRP + keywords, not on PepsiCo's item code or full
description, because those change every year (e.g. `50265 Lays ASCO 24g` in
2025 became `55403 Lays ASCO 26.5g` in 2026) and one code covers several
flavours. When several rules fit, the one with the most keywords wins; equally
specific rules pointing at different items are treated as unmapped and asked.
