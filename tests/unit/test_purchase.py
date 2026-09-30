from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tally_ai.purchase.build import PurchaseError, build_purchase_voucher, map_lines, remote_id
from tally_ai.purchase.learn import TallyBill, TallyLine, align, rules_from_evidence
from tally_ai.purchase.mapping import MappingFileError, MappingRule, MappingTable, leading_words
from tally_ai.purchase.pepsico import InvoiceParseError, parse_text
from tally_ai.tally.models import Side, VoucherKind
from tally_ai.tally.xml_builder import build_voucher

from .purchase_fixtures import LOCAL_ROWS, SUPPLIER_GSTIN, invoice_text, make_masters

D = Decimal


def local_invoice(**kw: object):  # type: ignore[no-untyped-def]
    return parse_text(invoice_text(**kw), "invoice.pdf")  # type: ignore[arg-type]


# ------------------------------------------------------------------ parsing


def test_parse_local_invoice() -> None:
    inv = local_invoice()
    assert (inv.invoice_number, inv.invoice_date, inv.supplier_gstin) == (
        "5460000001",
        date(2026, 10, 1),
        SUPPLIER_GSTIN,
    )
    assert not inv.interstate
    first, second = inv.lines
    assert (first.item_code, first.pack, first.mrp_per_piece, first.quantity) == ("55419", 300, D(5), D(10))
    assert (first.taxable, first.cgst, first.sgst, first.tax_rate) == (
        D("11550.00"),
        D("288.75"),
        D("288.75"),
        D(5),
    )
    assert first.description.startswith("Crunchy MM 18.7G RS 5 (300)50")
    # description running straight into the code ("SE55403")
    assert (second.item_code, second.pack, second.mrp_per_piece) == ("55403", 180, D(10))
    assert (inv.taxable, inv.cgst, inv.total) == (D("14315.54"), D("357.89"), D("15031.32"))
    assert inv.problems() == []


def test_parse_igst_invoice() -> None:
    inv = local_invoice(igst=True)
    assert inv.interstate
    assert [line.igst for line in inv.lines] == [D("577.50"), D("138.28")]
    assert (inv.igst, inv.cgst) == (D("715.78"), D(0))
    assert inv.problems() == []


def test_missing_line_is_caught_by_totals() -> None:
    rows = "\n".join(line for line in LOCAL_ROWS.splitlines() if not line.startswith("02 "))
    problems = local_invoice(rows=rows).problems()
    assert any("lines add up to taxable" in p for p in problems)


def test_wrong_tax_is_caught() -> None:
    rows = LOCAL_ROWS.replace("2.50 288.75 2.50 288.75 12,127.50", "2.50 300.00 2.50 288.75 12,138.75")
    assert any("is not 5.00% of" in p for p in local_invoice(rows=rows).problems())


def test_tcs_blocks_posting() -> None:
    rows = LOCAL_ROWS + "Add TCS 15.03\n"
    assert any("TCS" in p for p in local_invoice(rows=rows).problems())


def test_other_supplier_is_refused() -> None:
    with pytest.raises(InvoiceParseError, match="not a PepsiCo"):
        parse_text(["Some Other Company\nTax Invoice No. : 1"], "x.pdf")


# ------------------------------------------------------------------ mapping


def rules() -> list[MappingRule]:
    return [
        MappingRule("Crunchy", 300, D(5), "Crunchy (300) 5/-"),
        MappingRule("Chips", 180, D(10), "Chips (180) 10/-"),
        MappingRule("Chips Mini", 180, D(10), "Chips Mini (216) 10/-", D(2)),
    ]


def table(tmp_path: Path, items: list[MappingRule] | None = None) -> MappingTable:
    t = MappingTable(tmp_path / "pepsico_mapping.csv")
    for r in rules() if items is None else items:
        t.add(r)
    t.save()
    return t


def test_leading_words() -> None:
    assert leading_words("Lays ASCO 26.5g Rs 10(180)") == ["Lays", "ASCO"]
    assert leading_words("KK Playz Puff YC 16g") == ["KK", "Playz", "Puff"]


def test_most_specific_rule_wins(tmp_path: Path) -> None:
    t = table(tmp_path)
    inv = local_invoice()
    chips = inv.lines[1]
    assert t.match(chips).rule is not None and t.match(chips).rule.tally_item == "Chips (180) 10/-"  # type: ignore[union-attr]
    mini = chips.model_copy(update={"description": "Chips Mini Stix 30g"})
    outcome = t.match(mini)
    assert outcome.rule is not None and (outcome.rule.tally_item, outcome.rule.ctn_per_case) == (
        "Chips Mini (216) 10/-",
        D(2),
    )
    other_pack = chips.model_copy(update={"pack": 90})
    assert t.match(other_pack).rule is None


def test_equal_rules_to_different_items_conflict(tmp_path: Path) -> None:
    t = table(
        tmp_path,
        [
            MappingRule("Chips", 180, D(10), "Chips (180) 10/-"),
            MappingRule("Chips", 180, D(10), "Crunchy (180) 10/-"),
        ],
    )
    # add() replaces the same keywords+pack+MRP, so only the last one remains
    assert len(t.rules) == 1
    t2 = table(
        tmp_path / "b",
        [
            MappingRule("Chips ASCO", 180, D(10), "Chips (180) 10/-"),
            MappingRule("ASCO Chips", 180, D(10), "Crunchy (180) 10/-"),
        ],
    )
    outcome = t2.match(local_invoice().lines[1])
    assert outcome.rule is None and len(outcome.conflict) == 2


def test_csv_roundtrip_and_excel_edits(tmp_path: Path) -> None:
    t = table(tmp_path)
    text = t.path.read_text(encoding="utf-8-sig")
    assert text.splitlines()[0] == "keywords,pack,mrp,tally_item,ctn_per_case,source,updated"
    assert t.path.read_bytes().startswith(b"\xef\xbb\xbf")  # BOM so Excel reads UTF-8
    # user edits the file (e.g. in Excel): the change is picked up
    t.path.write_text(text.replace("Chips (180) 10/-", "Crunchy (180) 10/-"), encoding="utf-8-sig")
    import os

    os.utime(t.path, (1, 2_000_000_000))
    t.reload_if_changed()
    assert any(r.tally_item == "Crunchy (180) 10/-" for r in t.rules)


def test_invalid_csv_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "pepsico_mapping.csv"
    path.write_text("keywords,pack,mrp,tally_item\nChips,abc,10,X\n", encoding="utf-8")
    with pytest.raises(MappingFileError, match="'pack' must be a number"):
        MappingTable(path).reload_if_changed()


def test_locked_file_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    t = table(tmp_path)

    def locked(*args: object, **kwargs: object) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr("tally_ai.purchase.mapping.os.replace", locked)
    with pytest.raises(MappingFileError, match="close it in Excel"):
        t.save()


# ------------------------------------------------------------------ building the voucher


def test_purchase_voucher(tmp_path: Path) -> None:
    masters = make_masters()
    inv = local_invoice()
    mapped = map_lines(inv, table(tmp_path), masters)
    v = build_purchase_voucher(inv, mapped, masters)
    assert v.kind is VoucherKind.PURCHASE
    assert (v.party_ledger, v.reference, v.date, v.number) == (
        "Snacks Supplier (JH)",
        "5460000001",
        date(2026, 10, 1),
        None,
    )
    assert v.remote_id == remote_id(inv)
    assert [(i.stock_item, i.quantity, i.amount, i.rate) for i in v.inventory] == [
        ("Crunchy (300) 5/-", D(10), D("11550.00"), D("1155.00")),
        ("Chips (180) 10/-", D(2), D("2765.54"), D("1382.77")),
    ]
    assert [(line.ledger, line.side, line.amount) for line in v.ledgers] == [
        ("Snacks Supplier (JH)", Side.CREDIT, D("15031.32")),
        ("CGST", Side.DEBIT, D("357.89")),
        ("SGST", Side.DEBIT, D("357.89")),
    ]
    xml = build_voucher(v)
    assert xml.findtext("REFERENCE") == "5460000001"
    assert xml.find("VOUCHERNUMBER") is None  # Tally numbers it
    assert xml.findtext("ALLINVENTORYENTRIES.LIST/AMOUNT") == "-11550.00"
    assert xml.findtext("ALLINVENTORYENTRIES.LIST/ACCOUNTINGALLOCATIONS.LIST/LEDGERNAME") == "Purchase"


def test_exact_amount_is_kept_even_if_rate_rounds() -> None:
    """20 x 668.23 = 13364.60, but the invoice says 13364.54: the invoice wins."""
    from tally_ai.tally.models import InventoryLine

    line = InventoryLine(
        stock_item="X",
        quantity=D(20),
        unit="Ctn",
        rate=D("668.23"),
        accounting_ledger="Purchase",
        exact_amount=D("13364.54"),
    )
    assert line.amount == D("13364.54")


def test_factor_changes_quantity_not_amount(tmp_path: Path) -> None:
    masters = make_masters()
    inv = local_invoice()
    t = table(
        tmp_path,
        [
            MappingRule("Crunchy", 300, D(5), "Crunchy (300) 5/-", D(2)),
            MappingRule("Chips", 180, D(10), "Chips (180) 10/-"),
        ],
    )
    v = build_purchase_voucher(inv, map_lines(inv, t, masters), masters)
    first = v.inventory[0]
    assert (first.quantity, first.amount, first.rate) == (D(20), D("11550.00"), D("577.50"))


def test_igst_voucher_uses_wb_ledger(tmp_path: Path) -> None:
    masters = make_masters()
    inv = local_invoice(igst=True)
    v = build_purchase_voucher(inv, map_lines(inv, table(tmp_path), masters), masters)
    assert v.party_ledger == "Snacks Supplier (WB)"
    assert [(line.ledger, line.amount) for line in v.ledgers[1:]] == [("IGST", D("715.78"))]


def test_unmapped_or_bad_invoice_is_refused(tmp_path: Path) -> None:
    masters = make_masters()
    inv = local_invoice()
    only_one = table(tmp_path, [MappingRule("Crunchy", 300, D(5), "Crunchy (300) 5/-")])
    with pytest.raises(PurchaseError, match="not mapped"):
        build_purchase_voucher(inv, map_lines(inv, only_one, masters), masters)
    bad = local_invoice(rows="\n".join(x for x in LOCAL_ROWS.splitlines() if not x.startswith("02 ")))
    with pytest.raises(PurchaseError, match="does not add up"):
        build_purchase_voucher(bad, map_lines(bad, table(tmp_path / "b"), masters), masters)


def test_unknown_supplier_gstin(tmp_path: Path) -> None:
    masters = make_masters()
    masters.ledgers.pop("Snacks Supplier (JH)")
    inv = local_invoice()
    with pytest.raises(PurchaseError, match="GSTIN 20AAACP1272G1Z1"):
        build_purchase_voucher(inv, map_lines(inv, table(tmp_path), masters), masters)


def test_rule_to_deleted_tally_item_is_asked_again(tmp_path: Path) -> None:
    masters = make_masters()
    masters.items.pop("Chips (180) 10/-")
    mapped = map_lines(local_invoice(), table(tmp_path), masters)
    assert mapped[1].rule is None


# ------------------------------------------------------------------ learning from Tally


def test_align_handles_merged_lines_and_rounding() -> None:
    inv = local_invoice()
    # accountant merged nothing here but typed rate x qty (a few paise off), and entered 2 CAS as 4 Ctn
    bill = TallyBill(
        reference="5460000001",
        date=date(2026, 10, 1),
        party="Snacks Supplier (JH)",
        number="7",
        lines=[
            TallyLine("Crunchy (300) 5/-", D(10), D("11550.10")),
            TallyLine("Chips Mini (216) 10/-", D(4), D("2765.60")),
        ],
    )
    result = align(inv, bill)
    assert not result.unmatched_tally and not result.unmatched_invoice
    assert {(e.tally_item, e.factor) for e in result.evidence} == {
        ("Crunchy (300) 5/-", D(1)),
        ("Chips Mini (216) 10/-", D(2)),
    }


def test_learned_rules_generalise_only_with_two_flavours() -> None:
    inv = local_invoice()
    bill = TallyBill(
        "5460000001",
        date(2026, 10, 1),
        "S",
        "1",
        [
            TallyLine("Crunchy (300) 5/-", D(10), D("11550.00")),
            TallyLine("Chips (180) 10/-", D(2), D("2765.54")),
        ],
    )
    report = rules_from_evidence(align(inv, bill).evidence)
    by_item = {r.tally_item: r.keywords for r in report.rules}
    # one flavour seen each: rules stay flavour-specific
    assert by_item == {"Crunchy (300) 5/-": "Crunchy MM", "Chips (180) 10/-": "Chips ASCO"}
