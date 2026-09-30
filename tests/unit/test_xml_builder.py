import xml.etree.ElementTree as ET
from datetime import date

import pytest

from tally_ai.tally import VoucherKind
from tally_ai.tally.xml_builder import (
    build_collection_request,
    build_delete_voucher_request,
    build_import_request,
    build_voucher,
    tdl_string,
)

from .sample_vouchers import bank_receipt, purchase_invoice, sales_invoice


def entries(elem: ET.Element, tag: str) -> list[tuple[str | None, str | None, str | None]]:
    return [
        (e.findtext("LEDGERNAME"), e.findtext("ISDEEMEDPOSITIVE"), e.findtext("AMOUNT"))
        for e in elem.iter(tag)
    ]


def test_sales_invoice_header() -> None:
    v = build_voucher(sales_invoice())
    assert v.attrib == {"VCHTYPE": "Sales", "OBJVIEW": "Invoice Voucher View", "ACTION": "Create"}
    assert v.findtext("DATE") == "20261001"
    assert v.findtext("VOUCHERNUMBER") == "TEST/0001"
    assert v.findtext("PARTYLEDGERNAME") == "Example Traders"
    assert v.findtext("ISINVOICE") == "Yes"
    assert v.findtext("VCHENTRYMODE") == "Item Invoice"


def test_sales_invoice_signs_match_tally() -> None:
    """Debits are negative with ISDEEMEDPOSITIVE=Yes, as in Tally's own exports."""
    v = build_voucher(sales_invoice())
    assert entries(v, "LEDGERENTRIES.LIST") == [
        ("Example Traders", "Yes", "-18165.00"),
        ("CGST", "No", "432.51"),
        ("SGST", "No", "432.51"),
        ("Round Off (+/-)", "No", "-0.34"),
    ]
    inv = v.find("ALLINVENTORYENTRIES.LIST")
    assert inv is not None
    assert inv.findtext("STOCKITEMNAME") == "Crunchy Chips (300) 5/-"
    assert inv.findtext("ISDEEMEDPOSITIVE") == "No"
    assert inv.findtext("RATE") == "1310.63/Ctn"
    assert inv.findtext("DISCOUNT") == "12"
    assert inv.findtext("AMOUNT") == "17300.32"
    assert inv.findtext("ACTUALQTY") == "15 Ctn"
    assert inv.findtext("BATCHALLOCATIONS.LIST/GODOWNNAME") == "Main Location"
    assert entries(inv, "ACCOUNTINGALLOCATIONS.LIST") == [("Sales", "No", "17300.32")]


def test_inventory_precedes_ledger_entries() -> None:
    tags = [child.tag for child in build_voucher(sales_invoice())]
    assert tags.index("ALLINVENTORYENTRIES.LIST") < tags.index("LEDGERENTRIES.LIST")


def test_purchase_invoice_signs_and_reference() -> None:
    v = build_voucher(purchase_invoice())
    assert v.findtext("REFERENCE") == "SUP/123"
    assert v.findtext("REFERENCEDATE") == "20260930"
    assert entries(v, "LEDGERENTRIES.LIST") == [
        ("Snacks Co Distributors", "No", "10500.00"),
        ("CGST", "Yes", "-250.00"),
        ("SGST", "Yes", "-250.00"),
    ]
    inv = v.find("ALLINVENTORYENTRIES.LIST")
    assert inv is not None
    assert inv.findtext("ISDEEMEDPOSITIVE") == "Yes"
    assert inv.findtext("AMOUNT") == "-10000.00"
    assert inv.find("DISCOUNT") is None


def test_receipt_uses_accounting_view_and_bank_allocation() -> None:
    v = build_voucher(bank_receipt())
    assert v.attrib["OBJVIEW"] == "Accounting Voucher View"
    assert v.findtext("ISINVOICE") == "No"
    assert v.find("LEDGERENTRIES.LIST") is None
    assert entries(v, "ALLLEDGERENTRIES.LIST") == [
        ("Example Traders", "No", "5000.00"),
        ("Example Bank", "Yes", "-5000.00"),
    ]
    assert v.findtext("ALLLEDGERENTRIES.LIST/BANKALLOCATIONS.LIST/INSTRUMENTNUMBER") == "000123"


def test_no_number_lets_tally_assign_it() -> None:
    assert build_voucher(sales_invoice(number=None)).find("VOUCHERNUMBER") is None


def test_remote_id_enables_create_or_alter() -> None:
    v = build_voucher(sales_invoice(remote_id="abc-123"))
    assert v.attrib["REMOTEID"] == "abc-123"
    assert "ACTION" not in v.attrib


def test_special_characters_are_escaped() -> None:
    xml = build_import_request([sales_invoice(narration="Lay's & <Co>")])
    assert "Lay's &amp; &lt;Co&gt;" in xml
    assert ET.fromstring(xml).findtext(".//NARRATION") == "Lay's & <Co>"


def test_import_envelope() -> None:
    xml = build_import_request([sales_invoice()], company="Example Co")
    assert '<TALLYMESSAGE xmlns:UDF="TallyUDF">' in xml
    root = ET.fromstring(xml)
    assert root.findtext("HEADER/TALLYREQUEST") == "Import"
    assert root.findtext("BODY/DESC/STATICVARIABLES/SVCURRENTCOMPANY") == "Example Co"
    assert root.find("BODY/DATA/TALLYMESSAGE/VOUCHER") is not None


def test_import_envelope_without_company() -> None:
    root = ET.fromstring(build_import_request([sales_invoice()]))
    assert root.find("BODY/DESC/STATICVARIABLES/SVCURRENTCOMPANY") is None


def test_delete_request() -> None:
    root = ET.fromstring(build_delete_voucher_request(date(2026, 10, 1), VoucherKind.SALES, master_id=12702))
    v = root.find(".//VOUCHER")
    assert v is not None
    assert v.attrib == {
        "DATE": "20261001",
        "TAGNAME": "MasterID",
        "TAGVALUE": "12702",
        "ACTION": "Delete",
        "VCHTYPE": "Sales",
    }
    with pytest.raises(ValueError):
        build_delete_voucher_request(date(2026, 10, 1), "Sales")
    with pytest.raises(ValueError):
        build_delete_voucher_request(date(2026, 10, 1), "Sales", number="1", master_id=1)


def test_collection_request() -> None:
    root = ET.fromstring(
        build_collection_request(
            "Voucher",
            ["Date", "VoucherNumber"],
            filters=['$VoucherTypeName = "Sales"'],
            from_date=date(2025, 4, 1),
            to_date=date(2026, 3, 31),
        )
    )
    static = root.find("BODY/DESC/STATICVARIABLES")
    assert static is not None
    assert static.findtext("SVFROMDATE") == "20250401"
    assert static.findtext("SVTODATE") == "20260331"
    # Tally ignores the range without TYPE="Date"
    assert static.find("SVFROMDATE").get("TYPE") == "Date"  # type: ignore[union-attr]
    assert static.find("SVTODATE").get("TYPE") == "Date"  # type: ignore[union-attr]
    coll = root.find(".//COLLECTION")
    assert coll is not None
    assert coll.findtext("TYPE") == "Voucher"
    assert coll.findtext("FETCH") == "Date,VoucherNumber"
    assert coll.findtext("FILTER") == "Filter0"
    assert root.findtext(".//SYSTEM[@NAME='Filter0']") == '$VoucherTypeName = "Sales"'


def test_tdl_string() -> None:
    assert tdl_string("Lay's (180) 10/-") == '"Lay\'s (180) 10/-"'
    with pytest.raises(ValueError):
        tdl_string('bad "name"')
