"""LLM extraction of a sales message into structured fields, plus deterministic guards.

The LLM is asked to copy what the user wrote. Guards then drop anything the
message does not support (a rate with no rate marker, a quantity that does
not appear in the text), because small models often invent or misplace numbers.
"""

import contextlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Literal, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from tally_ai.agents.sales.spans import extend_to_typed, item_chunks, party_span

Intent = Literal["sales", "purchase", "receipt", "payment", "query", "other"]


class ExtractedItem(BaseModel):
    name: str = Field(description="Item name exactly as written, including pack size and MRP like '5/-'")
    quantity: float | None = Field(default=None, description="Number of units sold")
    unit: str | None = Field(default=None, description="Unit as written, e.g. ctn, pcs, box")
    rate: float | None = Field(default=None, description="Price per unit, only if the user states it")
    discount_pct: float | None = Field(default=None, description="Discount percent, only if stated")


class Extraction(BaseModel):
    intent: Intent
    party: str | None = Field(default=None, description="Customer name exactly as written")
    items: list[ExtractedItem] = Field(default_factory=list)
    date_text: str | None = Field(default=None, description="Date words as written, e.g. 'kal', '25th'")
    narration: str | None = None


SYSTEM_PROMPT = """You read short business messages from an Indian distributor (English, Hindi or \
Hinglish) and extract voucher details as JSON.

Rules:
- Copy customer and item names exactly as the user wrote them. Do not correct spelling or expand \
abbreviations.
- An item name includes its pack size and MRP. "5/-", "10/-", "5 rs" are part of the NAME (the MRP \
printed on the pack), never the rate. Example: "3 ctn KK 300 5/-" -> name "KK 300 5/-", quantity 3, \
unit "ctn".
- rate is set ONLY when the user gives a price per unit with a marker like "@", "rate", "rs per", \
"bhav". Otherwise null.
- discount_pct is set ONLY when the user states a discount ("12%", "less 12", "12 disc", "chhut"). \
Otherwise null.
- quantity is the count sold. unit is the word next to it (ctn, carton, pcs, box, pkt) or null.
- date_text copies date words ("aaj", "kal", "yesterday", "25th", "25/09") or null.
- intent: sales = sold / becha / diya / bheja / bill to a customer; purchase = bought from a supplier \
/ kharida; receipt = money received / mila / aaya; payment = money paid; query = a question; \
other = anything else.
- Several items can appear, joined by "aur", "and", "+" or commas.

Examples:
"sold 3 ctn Crunchy 300 5/- to Sharma ji" ->
{"intent":"sales","party":"Sharma ji","items":[{"name":"Crunchy 300 5/-","quantity":3,"unit":"ctn",\
"rate":null,"discount_pct":null}],"date_text":null,"narration":null}

"Gupta store ko 5 ctn masala puff 10/- @850 less 12% aur 2 ctn Mix Namkeen 400g bheja kal" ->
{"intent":"sales","party":"Gupta store","items":[{"name":"masala puff 10/-","quantity":5,"unit":"ctn",\
"rate":850,"discount_pct":12},{"name":"Mix Namkeen 400g","quantity":2,"unit":"ctn","rate":null,\
"discount_pct":null}],"date_text":"kal","narration":null}

"Verma general store tilta ko 10 ctn aloo bhujia 200g diya" ->
{"intent":"sales","party":"Verma general store tilta","items":[{"name":"aloo bhujia 200g",\
"quantity":10,"unit":"ctn","rate":null,"discount_pct":null}],"date_text":null,"narration":null}

"Sharma ji se 5000 cash mila" -> {"intent":"receipt","party":"Sharma ji","items":[],\
"date_text":null,"narration":null}
"""

UPDATE_PROMPT = (
    """The user is correcting a draft sales invoice. Apply the correction to the draft and \
return the complete updated draft as JSON in the same format. Keep everything the correction does \
not change. Item lines are numbered from 1 in the order given. Follow the same extraction rules:

"""
    + SYSTEM_PROMPT
)


class Extractor(Protocol):
    def extract(self, message: str) -> Extraction: ...

    def update(self, draft: Extraction, correction: str) -> Extraction: ...


_NUM = r"(\d+(?:\.\d+)?)"
# A stated rate: "@1150", "rate 1150", "1150 per ctn", "1150/ctn", "bhav 1150", "1150 ke rate"
_UNIT_WORD = r"(?:ctns?|cartons?|cartoons?|pcs|pc|piece|box|pkt|packet|kg)"
_RS = r"(?:rs\.?|₹)?\s*"
_SEP = r"(?:is|:|-|=)?\s*"
# A stated rate: "@840", "rate 840", "rate of ctn 840", "ctn rate 840", "rate per carton 840",
# "rate is rs 840", "840 per ctn", "840/ctn", "per ctn 840", "bhav 840", "840 ke rate"
_RATE_PATTERNS = [
    rf"@\s*{_RS}{_NUM}",
    # "(a|one|1)" needs a space after it, or "rate 1300" would read as 300
    rf"\brate\s*(?:of|per|for)?\s*(?:(?:a|one|1)\s+)?(?:{_UNIT_WORD}\s*)?{_SEP}{_RS}{_NUM}",
    rf"\b{_UNIT_WORD}\s*(?:ka|ke|ki)?\s*(?:rate|bhav)\s*{_SEP}{_RS}{_NUM}",
    rf"\bper\s*{_UNIT_WORD}\s*{_SEP}{_RS}{_NUM}",
    rf"\bbhav\s*{_SEP}{_RS}{_NUM}",
    rf"{_NUM}\s*{_RS}(?:per|/)\s*{_UNIT_WORD}\b",
    rf"{_NUM}\s*(?:ke|ka)\s*(?:rate|bhav)",
]
# A stated discount: "12%", "less 12", "12 less", "disc 12", "12 disc", "chhut 12"
_DISCOUNT_PATTERNS = [
    rf"{_NUM}\s*%",
    rf"\b(?:less|disc(?:ount)?|chh?ut|chhoot)\s*(?:of\s*)?{_NUM}",
    rf"{_NUM}\s*(?:less|disc(?:ount)?|chh?ut|chhoot)\b",
]
MAX_DISCOUNT = Decimal(100)


def _stated_values(patterns: list[str], text: str) -> set[Decimal]:
    values = set()
    for pattern in patterns:
        for m in re.finditer(pattern, text.replace(",", ""), re.I):
            with contextlib.suppress(InvalidOperation):
                values.add(Decimal(m.group(1)).normalize())
    return values


def _numbers_in(text: str) -> set[Decimal]:
    found = set()
    for n in re.findall(_NUM, text.replace(",", "")):
        with contextlib.suppress(InvalidOperation):
            found.add(Decimal(n).normalize())
    return found


def _mentions(value: float, numbers: set[Decimal]) -> bool:
    try:
        return Decimal(str(value)).normalize() in numbers
    except InvalidOperation:
        return False


def _restore_mrp(name: str, message: str) -> str:
    """Re-attach a trailing MRP ('5/-') the model split off the item name."""
    if re.search(r"\d\s*/-", name):
        return name
    idx = message.lower().find(name.lower())
    if idx < 0:
        return name
    rest = message[idx + len(name) :]
    m = re.match(r"\s*(\(?\d+\)?\s*/-)", rest)
    return f"{name} {m.group(1).strip()}" if m else name


def apply_guards(extraction: Extraction, message: str, previous: Extraction | None = None) -> Extraction:
    """Drop values the message does not support; repair split-off MRPs.

    With `previous` (a correction), values already in the previous draft are kept;
    only new values must be supported by the correction text.
    """
    numbers = _numbers_in(message)
    stated_rates = _stated_values(_RATE_PATTERNS, message)
    # A discount is a percentage: in "ctn 840 discount 5%" only 5 qualifies
    stated_discounts = {d for d in _stated_values(_DISCOUNT_PATTERNS, message) if 0 < d <= MAX_DISCOUNT}
    known = previous.items if previous else []
    known_qty = {i.quantity for i in known if i.quantity is not None}
    known_rate = {i.rate for i in known if i.rate is not None}
    known_disc = {i.discount_pct for i in known if i.discount_pct is not None}

    def keep(value: float | None, supported: set[Decimal], known_values: set[float]) -> float | None:
        if value is None:
            return None
        if value in known_values or _mentions(value, supported):
            return value
        return None

    items = [
        item.model_copy(
            update={
                "name": _restore_mrp(item.name.strip(), message),
                "quantity": keep(item.quantity, numbers, known_qty),
                "rate": keep(item.rate, stated_rates, known_rate),
                "discount_pct": keep(item.discount_pct, stated_discounts, known_disc),
            }
        )
        for item in extraction.items
    ]
    if previous is None:
        items = _extend_items(items, message)
    party = extraction.party
    if previous is None:
        span = party_span(message)
        if span:
            party = extend_to_typed(party, span, trailing_packs=False) if party else span
    date_text = extraction.date_text
    if date_text and previous is None and not _whole_words_in(date_text, message):
        date_text = None  # e.g. "kal" read out of "Kalpana"
    return extraction.model_copy(update={"items": items, "date_text": date_text, "party": party})


def _extend_items(items: list[ExtractedItem], message: str) -> list[ExtractedItem]:
    """Re-attach words the model dropped from item names, using '<qty> <unit> <text>' in the message."""
    chunks = item_chunks(message)
    used: set[int] = set()
    extended = []
    for item in items:
        name = item.name
        for index, (qty, typed) in enumerate(chunks):
            if index in used or (item.quantity is not None and qty != item.quantity):
                continue
            candidate = extend_to_typed(name, typed)
            if candidate != name or name.lower() == typed.lower():
                used.add(index)
                name = candidate
                break
        extended.append(item.model_copy(update={"name": name}))
    return extended


def _whole_words_in(phrase: str, message: str) -> bool:
    pattern = r"(?<!\w)" + r"\s+".join(re.escape(w) for w in phrase.split()) + r"(?!\w)"
    return re.search(pattern, message, re.I) is not None


class LLMExtractor:
    def __init__(self, model: BaseChatModel) -> None:
        self._structured = model.with_structured_output(Extraction)

    def _invoke(self, system: str, user: str) -> Extraction:
        result = self._structured.invoke([SystemMessage(content=system), HumanMessage(content=user)])
        if isinstance(result, Extraction):
            return result
        return Extraction.model_validate(result)

    def extract(self, message: str) -> Extraction:
        return apply_guards(self._invoke(SYSTEM_PROMPT, message), message)

    def update(self, draft: Extraction, correction: str) -> Extraction:
        user = f"Draft:\n{json.dumps(draft.model_dump(), ensure_ascii=False)}\n\nCorrection: {correction}"
        updated = self._invoke(UPDATE_PROMPT, user)
        return apply_guards(updated.model_copy(update={"intent": "sales"}), correction, previous=draft)
