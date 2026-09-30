# Sales voucher rules

How the agent turns a message like *"sold 3 ctn Crunchy 300 5/- to Sharma ji"*
into a Tally sales invoice. These rules were agreed with the business owner;
change them here first, then in code (`src/tally_ai/accounting`,
`src/tally_ai/agents/sales`).

The LLM only extracts what the user wrote (party text, item text, quantity,
unit, and a rate / discount / date only if stated). Everything below is
deterministic code.

| Topic | Rule |
|---|---|
| **Customer** | Searched only among ledgers under *Sundry Debtors*. |
| **Matching** | A clear single best match is used without asking; the confirmation still shows it. Otherwise the user picks from the top candidates (numbered list) or types a better name. |
| **Rate** | Taken from the message if stated (`@1310`, `rate 1310`). Otherwise the agent **always asks**, showing two hints: this customer's last rate and the item's last rate to anyone. Replying `same` takes the item's last rate; `customer` takes the customer's last rate. |
| **Discount** | From the message if stated (`12%`, `less 12`). Otherwise this customer's last discount on this item; **0 %** if they never bought it. |
| **GST** | Rate from Tally masters on the voucher date: the item's own GST details, else its stock group's. If none is found, the agent asks. Party in the company's state → CGST + SGST; other state → IGST. Tax is computed per line and rounded to paise, as Tally does. |
| **Sales ledger** | `Sales` for local customers; `Inter-State Sale` for IGST (configurable). |
| **Round off** | Total rounded to the nearest rupee (50 paise and above rounds up). A `Round Off` line is added only when the difference is not zero. |
| **Voucher number** | Computed by the agent: `<prefix>/<yy-yy>/<nnnn>`, highest number in the voucher date's financial year + 1. A new financial year starts at `0001`. Re-checked just before posting. |
| **Date** | Today, unless the message gives one (`aaj`, `kal`/`yesterday`, `parso`, `25th`, `25/09`, `25 sep`). Future dates and dates outside the current financial year are allowed but flagged. |
| **Stock** | If quantity exceeds closing stock, a warning is shown; posting is still allowed. |
| **Godown / batch** | `Main Location` / `Primary Batch` (configurable). |
| **Confirmation** | Always. Nothing is posted without an explicit `yes`. The user can reply with a correction (e.g. `rate 1300`, `qty 5`, `add 2 ctn ...`) or `no` to cancel. |
| **Retries** | Each draft carries a stable REMOTEID, so re-sending never creates a duplicate voucher. |

Out of scope for now: purchase, receipt and payment vouchers; alternate units.
