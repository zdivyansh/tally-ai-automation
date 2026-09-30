from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tally_ai.audit import AuditLog
from tally_ai.purchase import review as review_module
from tally_ai.purchase.learn import TallyBill, TallyLine
from tally_ai.purchase.mapping import MappingRule, MappingTable
from tally_ai.purchase.pepsico import parse_text
from tally_ai.purchase.review import ReviewContext, review_file
from tally_ai.purchase.state import PurchaseState
from tally_ai.purchase.watch import FolderWatcher
from tally_ai.tally.errors import TallyImportError
from tally_ai.tally.models import ImportResult, Voucher

from .purchase_fixtures import invoice_text, make_masters

D = Decimal


@dataclass
class FakeClient:
    posted: list[Voucher] = field(default_factory=list)
    reject: str | None = None

    def import_vouchers(self, vouchers: list[Voucher], *, check: bool = True) -> ImportResult:
        if self.reject:
            raise TallyImportError(ImportResult(exceptions=1, line_errors=[self.reject]))
        self.posted.extend(vouchers)
        return ImportResult(created=1)


class Script:
    """Answers questions in order and records what was asked / said."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []
        self.said: list[str] = []

    def ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        assert self.answers, f"unexpected question:\n{prompt}"
        return self.answers.pop(0)

    def say(self, text: str) -> None:
        self.said.append(text)


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    pdf = tmp_path / "5460000001.PDF"
    pdf.write_bytes(b"%PDF fake")
    invoice = parse_text(invoice_text(), str(pdf))
    monkeypatch.setattr(review_module, "parse_pdf", lambda path: invoice)
    in_tally: dict[str, TallyBill] = {}
    client = FakeClient()

    def find_bill(c: object, voucher_type: str, reference: str) -> TallyBill | None:
        if reference in in_tally:
            return in_tally[reference]
        if client.posted:
            return TallyBill(reference, date(2026, 10, 1), "Snacks Supplier (JH)", "21", [])
        return None

    monkeypatch.setattr(review_module, "find_bill", find_bill)
    mapping = MappingTable(tmp_path / "pepsico_mapping.csv")
    mapping.add(MappingRule("Crunchy", 300, D(5), "Crunchy (300) 5/-"))
    mapping.save()
    ctx = ReviewContext(
        masters=make_masters(),
        mapping=mapping,
        client=client,  # type: ignore[arg-type]
        state=PurchaseState(":memory:"),
        audit=AuditLog(":memory:"),
    )
    return {"pdf": pdf, "ctx": ctx, "client": client, "in_tally": in_tally, "mapping": mapping}


def test_new_product_is_asked_saved_and_posted(setup: dict[str, Any]) -> None:
    ctx, client = setup["ctx"], setup["client"]
    script = Script("1", "1", "yes")  # pick first suggestion, "all Chips (180) Rs10 products", post
    result = review_file(setup["pdf"], ctx, script.ask, script.say)

    question = script.asked[0]
    assert "New product on the invoice: Chips ASCO 26.5g Rs 10(180)" in question
    assert "1. Chips (180) 10/-" in question  # same pack and MRP ranked first
    assert "1. all 'Chips' Rs10 (180) products" in script.asked[1]
    assert "2. only 'Chips ASCO' Rs10 (180)" in script.asked[1]
    summary = script.asked[2]
    assert "Chips (180) 10/-: 2 Ctn x ₹1,382.77 = ₹2,765.54" in summary
    assert "Total     ₹15,031.32   (matches the invoice)" in summary

    assert result.outcome == "posted"
    assert "Posted purchase voucher 21 for invoice 5460000001" in result.message
    (voucher,) = client.posted
    assert voucher.reference == "5460000001"
    # the new rule is in the CSV, marked as yours
    saved = MappingTable(setup["mapping"].path)
    saved.reload_if_changed()
    assert any(
        r.keywords == "Chips" and r.tally_item == "Chips (180) 10/-" and r.source == "you"
        for r in saved.rules
    )
    # recorded: the same file is not offered again
    assert ctx.state.is_final(setup["pdf"])
    assert ctx.audit.postings()[0]["ok"] == 1


def test_scope_only_this_flavour(setup: dict[str, Any]) -> None:
    script = Script("1", "2", "no")
    review_file(setup["pdf"], setup["ctx"], script.ask, script.say)
    saved = MappingTable(setup["mapping"].path)
    saved.reload_if_changed()
    assert any(r.keywords == "Chips ASCO" for r in saved.rules)


def test_typed_search_finds_item(setup: dict[str, Any]) -> None:
    script = Script("chips mini", "1", "1", "no")
    review_file(setup["pdf"], setup["ctx"], script.ask, script.say)
    assert "1. Chips Mini (216) 10/-" in script.asked[1]


def test_no_skips_for_now_and_ignore_is_permanent(setup: dict[str, Any]) -> None:
    ctx = setup["ctx"]
    setup["mapping"].add(MappingRule("Chips", 180, D(10), "Chips (180) 10/-"))
    setup["mapping"].save()
    skipped = review_file(setup["pdf"], ctx, Script("no").ask, print)
    assert skipped.outcome == "skipped" and not ctx.state.is_final(setup["pdf"])
    ignored = review_file(setup["pdf"], ctx, Script("ignore").ask, print)
    assert ignored.outcome == "ignored" and ctx.state.is_final(setup["pdf"])
    assert setup["client"].posted == []


def test_skip_at_mapping_question(setup: dict[str, Any]) -> None:
    result = review_file(setup["pdf"], setup["ctx"], Script("skip").ask, print)
    assert result.outcome == "skipped"
    assert setup["client"].posted == []


def test_duplicate_in_tally_is_not_posted(setup: dict[str, Any]) -> None:
    setup["in_tally"]["5460000001"] = TallyBill(
        "5460000001",
        date(2026, 10, 1),
        "Snacks Supplier (JH)",
        "12",
        [TallyLine("Crunchy (300) 5/-", D(10), D(1))],
    )
    script = Script()
    result = review_file(setup["pdf"], setup["ctx"], script.ask, script.say)
    assert result.outcome == "duplicate"
    assert "already in Tally (voucher 12 dated 01-Oct-2026" in result.message
    assert script.asked == [] and setup["client"].posted == []


def test_tally_rejection_is_recorded(setup: dict[str, Any]) -> None:
    setup["client"].reject = "Voucher date is missing"
    setup["mapping"].add(MappingRule("Chips", 180, D(10), "Chips (180) 10/-"))
    setup["mapping"].save()
    result = review_file(setup["pdf"], setup["ctx"], Script("yes").ask, print)
    assert result.outcome == "failed" and "Voucher date is missing" in result.message
    posting = setup["ctx"].audit.postings()[0]
    assert posting["ok"] == 0 and "<REFERENCE>5460000001</REFERENCE>" in posting["request_xml"]
    assert not setup["ctx"].state.is_final(setup["pdf"])  # failed: tried again next start


def test_watcher_waits_until_download_finished(setup: dict[str, Any], tmp_path: Path) -> None:
    ctx = setup["ctx"]
    setup["mapping"].add(MappingRule("Chips", 180, D(10), "Chips (180) 10/-"))
    setup["mapping"].save()
    script = Script("yes")
    watcher = FolderWatcher(tmp_path, lambda: ctx, script.ask, script.say, interval=0)
    assert watcher.poll_once() == []  # first sight: size not yet known to be stable
    (result,) = watcher.poll_once()
    assert result.outcome == "posted"
    assert watcher.poll_once() == []  # done: not offered again
    assert any("Posted purchase voucher" in s for s in script.said)
