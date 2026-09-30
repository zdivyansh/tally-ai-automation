"""The sales draft: what is known so far, what still needs asking, and how answers apply.

Everything here is deterministic. Rules: docs/sales-rules.md.
"""

import datetime as dt
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from pydantic import BaseModel, Field

from tally_ai.accounting.dates import parse_user_date
from tally_ai.agents.sales.extraction import Extraction
from tally_ai.agents.sales.units import unit_key
from tally_ai.masters.cache import MasterData
from tally_ai.masters.matcher import MatchResult, normalize

MAX_CHOICES = 5


class RateSource(StrEnum):
    MESSAGE = "from message"
    USER = "entered"
    ITEM_LAST = "item's last rate"
    CUSTOMER_LAST = "customer's last rate"


class LineDraft(BaseModel):
    text: str
    quantity: Decimal | None = None
    unit_text: str | None = None
    item: str | None = None
    rate: Decimal | None = None
    rate_source: RateSource | None = None
    discount_pct: Decimal | None = Field(default=None, description="Only when stated by the user")
    gst_pct: Decimal | None = Field(default=None, description="Only when Tally has no GST rate")


class SalesDraft(BaseModel):
    extraction: Extraction
    party_text: str | None = None
    party: str | None = None
    date_text: str | None = None
    date: dt.date | None = None
    lines: list[LineDraft] = Field(default_factory=list)
    narration: str | None = None
    remote_id: str = Field(default_factory=lambda: str(uuid.uuid4()))


def _decimal(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def draft_from_extraction(extraction: Extraction) -> SalesDraft:
    return SalesDraft(
        extraction=extraction,
        party_text=extraction.party,
        date_text=extraction.date_text,
        narration=extraction.narration,
        lines=[
            LineDraft(
                text=item.name,
                quantity=_decimal(item.quantity),
                unit_text=item.unit,
                rate=_decimal(item.rate),
                rate_source=RateSource.MESSAGE if item.rate is not None else None,
                discount_pct=_decimal(item.discount_pct),
            )
            for item in extraction.items
        ],
    )


def merge_correction(old: SalesDraft, extraction: Extraction) -> SalesDraft:
    """Build a draft from a corrected extraction, keeping answers that still apply."""
    new = draft_from_extraction(extraction)
    new.remote_id = old.remote_id
    if old.party and new.party_text and normalize(new.party_text) == normalize(old.party_text or ""):
        new.party = old.party
    if new.date_text == old.date_text:
        new.date = old.date
    old_lines = {normalize(line.text): line for line in old.lines}
    for line in new.lines:
        previous = old_lines.get(normalize(line.text))
        if previous is None:
            continue
        line.item = previous.item
        line.gst_pct = previous.gst_pct
        if line.rate is None and previous.rate is not None:
            line.rate, line.rate_source = previous.rate, previous.rate_source
        if previous.unit_text is None and line.unit_text is not None and line.quantity == previous.quantity:
            line.unit_text = None  # unit was already converted to the item's unit
    return new


# ------------------------------------------------------------------ units


def unit_matches(unit_text: str | None, base_unit: str) -> bool:
    return unit_text is None or unit_key(unit_text) == unit_key(base_unit)


# ------------------------------------------------------------------ questions


class QuestionKind(StrEnum):
    DATE = "date"
    PARTY_NAME = "party_name"
    PARTY_CHOICE = "party_choice"
    ITEM_CHOICE = "item_choice"
    QUANTITY = "quantity"
    UNIT = "unit"
    RATE = "rate"
    GST = "gst"


@dataclass(frozen=True)
class Question:
    kind: QuestionKind
    line: int | None = None  # index into draft.lines
    choices: tuple[str, ...] = ()


def _match_party(draft: SalesDraft, masters: MasterData) -> MatchResult:
    return masters.party_matcher.match(draft.party_text or "", MAX_CHOICES)


def _match_item(line: LineDraft, masters: MasterData) -> MatchResult:
    return masters.item_matcher.match(line.text, MAX_CHOICES)


def auto_resolve(draft: SalesDraft, masters: MasterData, today: date) -> SalesDraft:
    """Fill in everything that needs no question: clear matches and dates."""
    draft = draft.model_copy(deep=True)
    if draft.date is None:
        draft.date = parse_user_date(draft.date_text, today)
    if draft.party is None and draft.party_text:
        result = _match_party(draft, masters)
        if result.is_confident and result.best:
            draft.party = result.best.name
    for line in draft.lines:
        if line.item is None:
            result = _match_item(line, masters)
            if result.is_confident and result.best:
                line.item = result.best.name
    return draft


def next_question(draft: SalesDraft, masters: MasterData) -> Question | None:
    """The next thing the user must answer, or None when the draft is complete."""
    if draft.date is None:
        return Question(QuestionKind.DATE)
    if draft.party is None:
        if not draft.party_text:
            return Question(QuestionKind.PARTY_NAME)
        return Question(
            QuestionKind.PARTY_CHOICE,
            choices=tuple(c.name for c in _match_party(draft, masters).candidates),
        )
    for i, line in enumerate(draft.lines):
        if line.item is None:
            return Question(
                QuestionKind.ITEM_CHOICE,
                line=i,
                choices=tuple(c.name for c in _match_item(line, masters).candidates),
            )
        item = masters.items[line.item]
        if line.quantity is None:
            return Question(QuestionKind.QUANTITY, line=i)
        if item.base_unit and not unit_matches(line.unit_text, item.base_unit):
            return Question(QuestionKind.UNIT, line=i)
        if line.rate is None:
            return Question(QuestionKind.RATE, line=i)
        if line.gst_pct is None and masters.gst_rate(item, draft.date) is None:
            return Question(QuestionKind.GST, line=i)
    return None


# ------------------------------------------------------------------ answers


class AnswerError(ValueError):
    """The answer does not fit the question; the message is shown to the user."""


def _positive_number(text: str, what: str) -> Decimal:
    cleaned = text.strip().lower().replace(",", "").replace("₹", "").removeprefix("rs").strip(" .")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise AnswerError(f"Please reply with a number for the {what}.") from None
    if value <= 0:
        raise AnswerError(f"The {what} must be more than zero.")
    return value


def apply_answer(
    draft: SalesDraft, question: Question, answer: str, masters: MasterData, today: date
) -> SalesDraft:
    draft = draft.model_copy(deep=True)
    text = answer.strip()
    if not text:
        raise AnswerError("Please type an answer (or 'cancel').")
    line = draft.lines[question.line] if question.line is not None else None

    match question.kind:
        case QuestionKind.DATE:
            parsed = parse_user_date(text, today)
            if parsed is None:
                raise AnswerError("I could not read that date. Try e.g. 25/09/2026, 'kal' or 'aaj'.")
            draft.date, draft.date_text = parsed, text

        case QuestionKind.PARTY_NAME:
            draft.party_text = text

        case QuestionKind.PARTY_CHOICE | QuestionKind.ITEM_CHOICE:
            chosen = _pick(text, question.choices)
            if question.kind is QuestionKind.PARTY_CHOICE:
                if chosen:
                    draft.party = chosen
                else:
                    draft.party_text = text  # search again with the new name
            else:
                assert line is not None
                if chosen:
                    line.item = chosen
                else:
                    line.text = text

        case QuestionKind.QUANTITY:
            assert line is not None
            line.quantity = _positive_number(text, "quantity")

        case QuestionKind.UNIT:
            assert line is not None
            line.quantity = _positive_number(text, "quantity")
            line.unit_text = None  # now in the item's own unit

        case QuestionKind.RATE:
            assert line is not None and line.item is not None and draft.party is not None
            word = text.lower()
            if word in {"same", "last", "wahi", "vahi", "same rate"}:
                last = masters.history.last_for_item(line.item)
                if last is None:
                    raise AnswerError("This item has no past sales. Please type the rate.")
                line.rate, line.rate_source = last.rate, RateSource.ITEM_LAST
            elif word in {"customer", "customer rate", "party", "party rate"}:
                last = masters.history.last_for_customer(draft.party, line.item)
                if last is None:
                    raise AnswerError("This customer has not bought this item before. Please type the rate.")
                line.rate, line.rate_source = last.rate, RateSource.CUSTOMER_LAST
            else:
                line.rate, line.rate_source = _positive_number(text, "rate"), RateSource.USER

        case QuestionKind.GST:
            assert line is not None
            cleaned = text.rstrip("%").strip()
            try:
                pct = Decimal(cleaned)
            except InvalidOperation:
                raise AnswerError("Please reply with the GST percent, e.g. 5, 12 or 18.") from None
            if not 0 <= pct <= 28:
                raise AnswerError("GST percent must be between 0 and 28.")
            line.gst_pct = pct
    return draft


def _pick(answer: str, choices: tuple[str, ...]) -> str | None:
    """A numbered choice ('2'), an exact name, or None to search again with the text."""
    if answer.isdigit():
        index = int(answer) - 1
        if not 0 <= index < len(choices):
            raise AnswerError(f"Please reply with a number from 1 to {len(choices)}, or type a name.")
        return choices[index]
    for choice in choices:
        if choice.lower() == answer.lower():
            return choice
    return None
