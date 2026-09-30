"""Whole conversations through the LangGraph agent, with a fake LLM and a fake Tally."""

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import pytest

from tally_ai.agents.sales import SalesAgent, SalesContext
from tally_ai.agents.sales.extraction import ExtractedItem, Extraction
from tally_ai.tally.errors import TallyImportError
from tally_ai.tally.masters import VoucherSummary
from tally_ai.tally.models import ImportResult, Side, Voucher

from .agent_fixtures import TODAY, make_masters, sale

D = Decimal
MSG = "sold 3 ctn crunchy 300 5/- to gupta store"
STATION = "GUPTA STORE (STATION)"


@dataclass
class FakeExtractor:
    by_message: dict[str, Extraction]
    by_correction: dict[str, Extraction] = field(default_factory=dict)

    def extract(self, message: str) -> Extraction:
        return self.by_message[message]

    def update(self, draft: Extraction, correction: str) -> Extraction:
        return self.by_correction[correction]


@dataclass
class FakeStore:
    numbers: list[str | None] = field(
        default_factory=lambda: ["TST/26-27/0006", "TST/26-27/0007", "TST/25-26/0850"]
    )
    posted: list[Voucher] = field(default_factory=list)
    reject: str | None = None

    def numbers_around(self, voucher_type: str, day: date) -> list[str | None]:
        return list(self.numbers)

    def post(self, voucher: Voucher) -> ImportResult:
        if self.reject:
            raise TallyImportError(ImportResult(exceptions=1, line_errors=[self.reject]))
        assert voucher.number
        self.posted.append(voucher)
        self.numbers.append(voucher.number)
        return ImportResult(created=1)

    def find(self, voucher_type: str, number: str, day: date) -> list[VoucherSummary]:
        return [VoucherSummary(date=day, voucher_type=voucher_type, number=number, master_id=99)]


def make_agent(store: FakeStore, extractor: FakeExtractor | None = None) -> SalesAgent:
    extractor = extractor or FakeExtractor({MSG: sale("gupta store", ("Crunchy 300 5/-", 3))})
    return SalesAgent(
        SalesContext(masters=make_masters(), extractor=extractor, store=store, today=lambda: TODAY)
    )


def choice(text: str, name: str) -> str:
    """The number shown next to `name` in a numbered question."""
    for line in text.splitlines():
        number, _, rest = line.strip().partition(". ")
        if number.isdigit() and rest == name:
            return number
    raise AssertionError(f"{name!r} not offered in:\n{text}")


class Chat:
    """One conversation thread."""

    def __init__(self, agent: SalesAgent) -> None:
        self.agent = agent
        self.thread = str(uuid.uuid4())
        self.done = False

    def start(self, message: str = MSG) -> str:
        turn = self.agent.start(message, self.thread)
        self.done = turn.done
        return turn.text

    def say(self, answer: str) -> str:
        assert not self.done, "conversation already finished"
        turn = self.agent.reply(answer, self.thread)
        self.done = turn.done
        return turn.text

    def to_rate_question(self, customer: str = STATION) -> str:
        return self.say(choice(self.start(), customer))


def test_full_sale_with_questions() -> None:
    store = FakeStore()
    chat = Chat(make_agent(store))

    first = chat.start()
    assert "Which customer is 'gupta store'?" in first
    assert all(name in first for name in ("Gupta Store", "Gupta Store (Main Road)", STATION))

    rate_q = chat.say(choice(first, STATION))
    assert "Rate for Crunchy (300) 5/- (3 Ctn)?" in rate_q
    assert f"last to {STATION}: ₹1,310.63/Ctn on 28-Sep-2026" in rate_q
    assert "last to anyone: ₹1,310.63/Ctn on 28-Sep-2026" in rate_q

    summary = chat.say("customer")
    assert "Sales invoice TST/26-27/0008  |  30-Sep-2026" in summary
    assert "3 Ctn x ₹1,310.63 less 11% = ₹3,499.38" in summary
    assert "rate: customer's last rate (you chose); discount: customer's last (28-Sep-2026)" in summary
    assert "CGST 2.5%" in summary and "SGST 2.5%" in summary
    assert "Total       ₹3,674.00" in summary
    assert "Warning: Stock of Crunchy (300) 5/- is -40 Ctn; this sale is 3." in summary
    assert not chat.done

    result = chat.say("yes")
    assert chat.done
    assert result == f"Posted sales invoice TST/26-27/0008 for {STATION}, total ₹3,674.00."
    (voucher,) = store.posted
    assert voucher.number == "TST/26-27/0008"
    assert voucher.party_ledger == STATION
    assert voucher.side_total(Side.DEBIT) == voucher.side_total(Side.CREDIT) == D(3674)
    assert voucher.remote_id


@pytest.mark.parametrize("name", ["Gupta Store", "Gupta Store (Main Road)", STATION])
def test_number_picks_what_was_shown(name: str) -> None:
    rate_q = Chat(make_agent(FakeStore())).to_rate_question(name)
    assert f"last to {name}" in rate_q or f"{name} has not bought" in rate_q


def test_new_customer_gets_zero_discount() -> None:
    chat = Chat(make_agent(FakeStore()))
    rate_q = chat.to_rate_question("Gupta Store")
    assert "Gupta Store has not bought this item before" in rate_q
    assert "'customer'" not in rate_q  # no customer rate to offer
    summary = chat.say("same")
    assert "less" not in summary.split("\n")[4]
    assert "discount: none (first purchase of this item)" in summary


def test_cancel_at_question() -> None:
    store = FakeStore()
    chat = Chat(make_agent(store))
    chat.start()
    assert chat.say("cancel") == "Cancelled. Nothing was posted."
    assert chat.done and store.posted == []


def test_no_at_confirmation() -> None:
    store = FakeStore()
    chat = Chat(make_agent(store))
    chat.to_rate_question()
    chat.say("same")
    assert chat.say("nahi") == "Cancelled. Nothing was posted."
    assert store.posted == []


def test_invalid_answer_is_explained_and_reasked() -> None:
    chat = Chat(make_agent(FakeStore()))
    chat.to_rate_question()
    again = chat.say("cheap")
    assert again.startswith("Please reply with a number for the rate.")
    assert "Rate for Crunchy (300) 5/-" in again
    chat.say("1300")
    assert chat.say("yes").startswith("Posted")


def test_correction_at_confirmation() -> None:
    corrected = Extraction(
        intent="sales",
        party="gupta store",
        items=[ExtractedItem(name="Crunchy 300 5/-", quantity=5, unit="ctn", rate=1300)],
    )
    extractor = FakeExtractor(
        {MSG: sale("gupta store", ("Crunchy 300 5/-", 3))}, {"qty 5 rate 1300": corrected}
    )
    store = FakeStore()
    chat = Chat(make_agent(store, extractor))
    chat.to_rate_question()
    chat.say("same")
    summary = chat.say("qty 5 rate 1300")
    assert f"Customer: {STATION}" in summary  # earlier choice kept, not asked again
    assert "5 Ctn x ₹1,300.00 less 11%" in summary
    assert chat.say("yes").startswith("Posted")
    assert store.posted[0].inventory[0].quantity == D(5)


def test_number_is_rechecked_before_posting() -> None:
    store = FakeStore()
    chat = Chat(make_agent(store))
    chat.to_rate_question()
    assert "TST/26-27/0008" in chat.say("same")
    store.numbers.append("TST/26-27/0008")  # someone billed in Tally meanwhile
    result = chat.say("yes")
    assert "TST/26-27/0009" in result
    assert "number changed from TST/26-27/0008" in result


def test_tally_rejection_is_reported() -> None:
    chat = Chat(make_agent(FakeStore(reject="Ledger 'X' does not exist!")))
    chat.to_rate_question()
    chat.say("same")
    assert (
        chat.say("yes") == "Tally did not accept the invoice: Tally import failed: Ledger 'X' does not exist!"
    )


def test_non_sales_intent() -> None:
    extractor = FakeExtractor({"sharma se 5000 mila": Extraction(intent="receipt", party="sharma")})
    chat = Chat(make_agent(FakeStore(), extractor))
    assert "Only sales invoices are supported" in chat.start("sharma se 5000 mila")
    assert chat.done


def test_history_updates_after_posting() -> None:
    agent = make_agent(FakeStore())
    first = Chat(agent)
    first.to_rate_question()
    first.say("1250")
    first.say("yes")
    rate_q = Chat(agent).to_rate_question()
    assert f"last to {STATION}: ₹1,250.00/Ctn on 30-Sep-2026" in rate_q


@pytest.mark.parametrize("word", ["yes", "Haan", "OK", "post"])
def test_yes_words(word: str) -> None:
    store = FakeStore()
    chat = Chat(make_agent(store))
    chat.to_rate_question()
    chat.say("same")
    assert chat.say(word).startswith("Posted")
    assert len(store.posted) == 1
