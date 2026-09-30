"""Build Tally XML requests. No string templating: ElementTree handles escaping."""

import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from datetime import date

from tally_ai.tally.models import InventoryLine, LedgerLine, Side, Voucher, VoucherKind
from tally_ai.tally.parsing import format_amount, format_date, format_number


def _sub(parent: ET.Element, tag: str, value: str | None) -> None:
    if value is not None:
        ET.SubElement(parent, tag).text = value


def _yes_no(flag: bool) -> str:
    return "Yes" if flag else "No"


def _tally_amount(side: Side, amount_on_side: str) -> str:
    """Tally stores debits as negative amounts."""
    if side is Side.DEBIT:
        return amount_on_side[1:] if amount_on_side.startswith("-") else f"-{amount_on_side}"
    return amount_on_side


def _ledger_entry(parent: ET.Element, tag: str, line: LedgerLine) -> None:
    entry = ET.SubElement(parent, tag)
    _sub(entry, "LEDGERNAME", line.ledger)
    _sub(entry, "ISDEEMEDPOSITIVE", _yes_no(line.side is Side.DEBIT))
    _sub(entry, "ISPARTYLEDGER", _yes_no(line.is_party))
    _sub(entry, "AMOUNT", _tally_amount(line.side, format_amount(line.amount)))
    if line.bank:
        bank = ET.SubElement(entry, "BANKALLOCATIONS.LIST")
        _sub(bank, "TRANSACTIONTYPE", line.bank.transaction_type)
        _sub(bank, "INSTRUMENTNUMBER", line.bank.instrument_number)
        if line.bank.instrument_date:
            _sub(bank, "INSTRUMENTDATE", format_date(line.bank.instrument_date))
        _sub(bank, "PAYMENTFAVOURING", line.bank.favouring)
        _sub(bank, "AMOUNT", _tally_amount(line.side, format_amount(line.amount)))


def _inventory_entry(parent: ET.Element, line: InventoryLine, side: Side) -> None:
    amount = _tally_amount(side, format_amount(line.amount))
    qty = f"{format_number(line.quantity)} {line.unit}"

    entry = ET.SubElement(parent, "ALLINVENTORYENTRIES.LIST")
    _sub(entry, "STOCKITEMNAME", line.stock_item)
    _sub(entry, "ISDEEMEDPOSITIVE", _yes_no(side is Side.DEBIT))
    _sub(entry, "RATE", f"{format_amount(line.rate)}/{line.unit}")
    if line.discount_pct:
        _sub(entry, "DISCOUNT", format_number(line.discount_pct))
    _sub(entry, "AMOUNT", amount)
    _sub(entry, "ACTUALQTY", qty)
    _sub(entry, "BILLEDQTY", qty)

    if line.godown or line.batch:
        batch = ET.SubElement(entry, "BATCHALLOCATIONS.LIST")
        _sub(batch, "GODOWNNAME", line.godown)
        _sub(batch, "BATCHNAME", line.batch)
        _sub(batch, "AMOUNT", amount)
        _sub(batch, "ACTUALQTY", qty)
        _sub(batch, "BILLEDQTY", qty)

    alloc = ET.SubElement(entry, "ACCOUNTINGALLOCATIONS.LIST")
    _sub(alloc, "LEDGERNAME", line.accounting_ledger)
    _sub(alloc, "ISDEEMEDPOSITIVE", _yes_no(side is Side.DEBIT))
    _sub(alloc, "AMOUNT", amount)


def build_voucher(voucher: Voucher) -> ET.Element:
    attrs = {"VCHTYPE": voucher.type_name, "OBJVIEW": voucher.kind.view}
    if voucher.remote_id:
        # No ACTION: Tally creates the voucher, or alters it if the REMOTEID exists
        attrs["REMOTEID"] = voucher.remote_id
    else:
        attrs["ACTION"] = "Create"
    elem = ET.Element("VOUCHER", attrs)

    _sub(elem, "DATE", format_date(voucher.date))
    _sub(elem, "EFFECTIVEDATE", format_date(voucher.date))
    _sub(elem, "VOUCHERTYPENAME", voucher.type_name)
    _sub(elem, "VOUCHERNUMBER", voucher.number)
    _sub(elem, "REFERENCE", voucher.reference)
    if voucher.reference_date:
        _sub(elem, "REFERENCEDATE", format_date(voucher.reference_date))
    _sub(elem, "PARTYLEDGERNAME", voucher.party_ledger)
    _sub(elem, "PARTYNAME", voucher.party_ledger)
    _sub(elem, "STATENAME", voucher.state_name)
    _sub(elem, "PLACEOFSUPPLY", voucher.place_of_supply)
    _sub(elem, "PARTYGSTIN", voucher.party_gstin)
    _sub(elem, "GSTREGISTRATIONTYPE", voucher.gst_registration_type)
    _sub(elem, "NARRATION", voucher.narration)
    _sub(elem, "PERSISTEDVIEW", voucher.kind.view)
    _sub(elem, "ISINVOICE", _yes_no(voucher.kind.is_invoice))
    if voucher.kind.is_invoice:
        _sub(elem, "VCHENTRYMODE", "Item Invoice" if voucher.inventory else "Accounting Invoice")

    if voucher.kind.is_invoice:
        for inv in voucher.inventory:
            _inventory_entry(elem, inv, voucher.kind.inventory_side)
        for line in voucher.ledgers:
            _ledger_entry(elem, "LEDGERENTRIES.LIST", line)
    else:
        for line in voucher.ledgers:
            _ledger_entry(elem, "ALLLEDGERENTRIES.LIST", line)
    return elem


def _static_variables(
    parent: ET.Element,
    company: str | None,
    extra: Mapping[str, str] | None = None,
    dates: Mapping[str, date] | None = None,
) -> None:
    static = ET.SubElement(parent, "STATICVARIABLES")
    for tag, value in (extra or {}).items():
        _sub(static, tag, value)
    for tag, day in (dates or {}).items():
        # Without TYPE="Date" Tally silently ignores the value and uses the current period
        ET.SubElement(static, tag, {"TYPE": "Date"}).text = format_date(day)
    _sub(static, "SVCURRENTCOMPANY", company)


def _envelope(tally_request: str, request_type: str, request_id: str) -> tuple[ET.Element, ET.Element]:
    envelope = ET.Element("ENVELOPE")
    header = ET.SubElement(envelope, "HEADER")
    _sub(header, "VERSION", "1")
    _sub(header, "TALLYREQUEST", tally_request)
    _sub(header, "TYPE", request_type)
    _sub(header, "ID", request_id)
    return envelope, ET.SubElement(envelope, "BODY")


def to_string(elem: ET.Element) -> str:
    return ET.tostring(elem, encoding="unicode")


def _import_request(voucher_elems: Iterable[ET.Element], company: str | None) -> str:
    envelope, body = _envelope("Import", "Data", "Vouchers")
    _static_variables(ET.SubElement(body, "DESC"), company)
    message = ET.SubElement(ET.SubElement(body, "DATA"), "TALLYMESSAGE", {"xmlns:UDF": "TallyUDF"})
    message.extend(voucher_elems)
    return to_string(envelope)


def build_import_request(vouchers: Sequence[Voucher], company: str | None = None) -> str:
    return _import_request((build_voucher(v) for v in vouchers), company)


def build_delete_voucher_request(
    voucher_date: date,
    kind: VoucherKind | str,
    *,
    number: str | None = None,
    master_id: int | None = None,
    company: str | None = None,
) -> str:
    """Delete one voucher, identified by MasterID (preferred, unique) or voucher number."""
    if (number is None) == (master_id is None):
        raise ValueError("give exactly one of number or master_id")
    type_name = kind.value if isinstance(kind, VoucherKind) else kind
    tag_name, tag_value = (
        ("MasterID", str(master_id)) if master_id is not None else ("Voucher Number", number)
    )
    elem = ET.Element(
        "VOUCHER",
        {
            "DATE": format_date(voucher_date),
            "TAGNAME": tag_name,
            "TAGVALUE": tag_value or "",
            "ACTION": "Delete",
            "VCHTYPE": type_name,
        },
    )
    _sub(elem, "VOUCHERTYPENAME", type_name)
    return _import_request([elem], company)


def build_collection_request(
    object_type: str,
    fetch: Sequence[str],
    *,
    filters: Sequence[str] = (),
    from_date: date | None = None,
    to_date: date | None = None,
    company: str | None = None,
) -> str:
    """Export a TDL collection of `object_type`, optionally filtered by TDL formulae.

    Vouchers are only exported for the current period unless a date range is given.
    """
    envelope, body = _envelope("Export", "Collection", "Coll")
    desc = ET.SubElement(body, "DESC")
    dates = {"SVFROMDATE": from_date, "SVTODATE": to_date}
    _static_variables(
        desc,
        company,
        {"SVEXPORTFORMAT": "$$SysName:XML"},
        {tag: day for tag, day in dates.items() if day is not None},
    )

    tdl_message = ET.SubElement(ET.SubElement(desc, "TDL"), "TDLMESSAGE")
    collection = ET.SubElement(tdl_message, "COLLECTION", {"NAME": "Coll", "ISMODIFY": "No"})
    _sub(collection, "TYPE", object_type)
    _sub(collection, "FETCH", ",".join(fetch))
    names = [f"Filter{i}" for i in range(len(filters))]
    if names:
        _sub(collection, "FILTER", ",".join(names))
    for name, formula in zip(names, filters, strict=True):
        ET.SubElement(tdl_message, "SYSTEM", {"TYPE": "Formulae", "NAME": name}).text = formula
    return to_string(envelope)


def tdl_string(value: str) -> str:
    """Quote a value for use inside a TDL formula."""
    if '"' in value:
        raise ValueError(f"double quotes are not supported in TDL filter values: {value!r}")
    return f'"{value}"'
