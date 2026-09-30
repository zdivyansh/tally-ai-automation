"""Read-only queries against Tally, returning typed master data."""

import xml.etree.ElementTree as ET
from datetime import date

from tally_ai.tally.client import TallyClient
from tally_ai.tally.masters import (
    Company,
    Group,
    GstDetails,
    GstRate,
    Ledger,
    StockGroup,
    StockItem,
    VoucherSummary,
    VoucherTypeInfo,
    resolve_group_path,
)
from tally_ai.tally.parsing import parse_date, parse_decimal, text
from tally_ai.tally.xml_builder import tdl_string

# Wide enough to cover every financial year a company can hold
ALL_DATES = (date(2000, 1, 1), date(2099, 12, 31))

_DUTY_HEADS = {"IGST": "igst", "CGST": "cgst", "SGST/UTGST": "sgst", "Cess": "cess"}


def _name(elem: ET.Element) -> str | None:
    name = elem.get("NAME")
    return name.strip() if name and name.strip() else None


def _parent(elem: ET.Element) -> str | None:
    return text(elem, "PARENT")


def _aliases(elem: ET.Element, name: str) -> tuple[str, ...]:
    """Alternate names from LANGUAGENAME.LIST/NAME.LIST (the first entry is the name itself)."""
    names = [
        n.text.strip()
        for lang in elem.findall("LANGUAGENAME.LIST")
        for names_list in lang.findall("NAME.LIST")
        for n in names_list.findall("NAME")
        if n.text and n.text.strip()
    ]
    return tuple(dict.fromkeys(n for n in names if n != name))


def _int(value: str | None) -> int | None:
    number = parse_decimal(value)
    return int(number) if number is not None else None


def parse_gst_details(elem: ET.Element) -> GstDetails:
    source: str | None = None
    rates: list[GstRate] = []
    for details in elem.findall("GSTDETAILS.LIST"):
        applicable_from = parse_date(text(details, "APPLICABLEFROM"))
        if applicable_from is None:
            continue
        source = text(details, "SRCOFGSTDETAILS") or source
        heads: dict[str, object] = {}
        for state in details.findall("STATEWISEDETAILS.LIST"):
            for rate in state.findall("RATEDETAILS.LIST"):
                field = _DUTY_HEADS.get(text(rate, "GSTRATEDUTYHEAD") or "")
                value = parse_decimal(text(rate, "GSTRATE"))
                if field and value is not None:
                    heads[field] = value
        rates.append(
            GstRate(applicable_from=applicable_from, taxability=text(details, "TAXABILITY"), **heads)
        )
    return GstDetails(source=source, rates=tuple(sorted(rates, key=lambda r: r.applicable_from)))


class TallyQueries:
    def __init__(self, client: TallyClient) -> None:
        self.client = client

    def companies(self) -> list[Company]:
        root = self.client.export_collection(
            "Company", ["Name", "StartingFrom", "BooksFrom", "GSTIN", "StateName"]
        )
        return [
            Company(
                name=name,
                starting_from=parse_date(text(c, "STARTINGFROM")),
                books_from=parse_date(text(c, "BOOKSFROM")),
                gstin=text(c, "GSTIN"),
                state=text(c, "STATENAME"),
            )
            for c in root.iter("COMPANY")
            if (name := _name(c))
        ]

    def groups(self) -> dict[str, Group]:
        root = self.client.export_collection("Group", ["Name", "Parent"])
        return {name: Group(name=name, parent=_parent(g)) for g in root.iter("GROUP") if (name := _name(g))}

    def ledgers(self) -> list[Ledger]:
        groups = self.groups()
        root = self.client.export_collection(
            "Ledger",
            [
                "Name",
                "Parent",
                "LanguageName",
                "LedStateName",
                "PartyGSTIN",
                "GSTRegistrationType",
                "GSTDutyHead",
            ],
        )
        ledgers = []
        for elem in root.iter("LEDGER"):
            name = _name(elem)
            if not name:
                continue
            parent = _parent(elem)
            ledgers.append(
                Ledger(
                    name=name,
                    parent=parent,
                    group_path=resolve_group_path(parent, groups),
                    aliases=_aliases(elem, name),
                    state=text(elem, "LEDSTATENAME"),
                    gstin=text(elem, "PARTYGSTIN"),
                    gst_registration_type=text(elem, "GSTREGISTRATIONTYPE"),
                    gst_duty_head=text(elem, "GSTDUTYHEAD"),
                )
            )
        return ledgers

    def stock_groups(self) -> dict[str, StockGroup]:
        root = self.client.export_collection("StockGroup", ["Name", "Parent", "GSTDetails.*"])
        return {
            name: StockGroup(name=name, parent=_parent(g), gst=parse_gst_details(g))
            for g in root.iter("STOCKGROUP")
            if (name := _name(g))
        }

    def stock_items(self) -> list[StockItem]:
        root = self.client.export_collection(
            "StockItem", ["Name", "Parent", "LanguageName", "BaseUnits", "GSTDetails.*", "HSNDetails.*"]
        )
        items = []
        for elem in root.iter("STOCKITEM"):
            name = _name(elem)
            if not name:
                continue
            hsn_codes = [
                (parse_date(text(h, "APPLICABLEFROM")) or date.min, code)
                for h in elem.findall("HSNDETAILS.LIST")
                if (code := text(h, "HSNCODE"))
            ]
            items.append(
                StockItem(
                    name=name,
                    parent=_parent(elem),
                    aliases=_aliases(elem, name),
                    base_unit=text(elem, "BASEUNITS"),
                    hsn=max(hsn_codes)[1] if hsn_codes else None,
                    gst=parse_gst_details(elem),
                )
            )
        return items

    def voucher_types(self) -> list[VoucherTypeInfo]:
        root = self.client.export_collection("VoucherType", ["Name", "Parent", "NumberingMethod"])
        return [
            VoucherTypeInfo(name=name, parent=_parent(v), numbering_method=text(v, "NUMBERINGMETHOD"))
            for v in root.iter("VOUCHERTYPE")
            if (name := _name(v))
        ]

    def vouchers(
        self,
        *,
        voucher_type: str | None = None,
        number: str | None = None,
        from_date: date = ALL_DATES[0],
        to_date: date = ALL_DATES[1],
    ) -> list[VoucherSummary]:
        """Voucher headers in a date range (default: all dates), newest first."""
        filters = []
        if voucher_type:
            filters.append(f"$VoucherTypeName = {tdl_string(voucher_type)}")
        if number:
            filters.append(f"$VoucherNumber = {tdl_string(number)}")
        root = self.client.export_collection(
            "Voucher",
            ["Date", "VoucherTypeName", "VoucherNumber", "PartyLedgerName", "MasterID", "AlterID"],
            filters=filters,
            from_date=from_date,
            to_date=to_date,
        )
        summaries = []
        for v in root.iter("VOUCHER"):
            vdate = parse_date(text(v, "DATE"))
            vtype = text(v, "VOUCHERTYPENAME")
            if vdate is None or vtype is None:
                continue
            summaries.append(
                VoucherSummary(
                    date=vdate,
                    voucher_type=vtype,
                    number=text(v, "VOUCHERNUMBER"),
                    party_ledger=text(v, "PARTYLEDGERNAME"),
                    master_id=_int(text(v, "MASTERID")),
                    alter_id=_int(text(v, "ALTERID")),
                )
            )
        return sorted(summaries, key=lambda s: (s.date, s.master_id or 0), reverse=True)
