"""Read-only models of Tally master data."""

from collections.abc import Mapping
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

PRIMARY = "Primary"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class Company(_Model):
    name: str
    starting_from: date | None = None
    books_from: date | None = None
    gstin: str | None = None
    state: str | None = None


class Group(_Model):
    """Accounting group (Sundry Debtors, Duties & Taxes, ...)."""

    name: str
    parent: str | None = None


class GstRate(_Model):
    applicable_from: date
    taxability: str | None = None
    igst: Decimal = Decimal(0)
    cgst: Decimal = Decimal(0)
    sgst: Decimal = Decimal(0)
    cess: Decimal = Decimal(0)


class GstDetails(_Model):
    """GST history of an item or group. `source` says whether rates are set here."""

    source: str | None = None
    rates: tuple[GstRate, ...] = ()

    @property
    def is_specified_here(self) -> bool:
        # "Specify Details Here"; the alternative is "As per Company/Stock Group"
        return self.source is not None and self.source.lower().startswith("specify")

    def rate_on(self, on: date) -> GstRate | None:
        applicable = [rate for rate in self.rates if rate.applicable_from <= on]
        return max(applicable, key=lambda rate: rate.applicable_from) if applicable else None


class Ledger(_Model):
    name: str
    parent: str | None = None
    group_path: tuple[str, ...] = Field(
        default=(), description="Parent group first, then its ancestors up to the top-level group"
    )
    aliases: tuple[str, ...] = ()
    state: str | None = None
    gstin: str | None = None
    gst_registration_type: str | None = None
    gst_duty_head: str | None = Field(default=None, description="CGST / SGST/UTGST / IGST for tax ledgers")

    def is_under(self, group: str) -> bool:
        """True if the ledger sits anywhere below `group`, e.g. 'Sundry Debtors'."""
        return group in self.group_path


class StockGroup(_Model):
    name: str
    parent: str | None = None
    gst: GstDetails = GstDetails()


class StockItem(_Model):
    name: str
    parent: str | None = None
    aliases: tuple[str, ...] = ()
    base_unit: str | None = None
    hsn: str | None = None
    gst: GstDetails = GstDetails()


class VoucherTypeInfo(_Model):
    name: str
    parent: str | None = None
    numbering_method: str | None = None


class VoucherSummary(_Model):
    date: date
    voucher_type: str
    number: str | None = None
    party_ledger: str | None = None
    master_id: int | None = None
    alter_id: int | None = None


def resolve_group_path(parent: str | None, groups: Mapping[str, Group]) -> tuple[str, ...]:
    """Groups from `parent` up to the top level.

    e.g. ('Retail Customers', 'Sundry Debtors', 'Current Assets')
    """
    path: list[str] = []
    current = parent
    while current and current != PRIMARY and current not in path:
        path.append(current)
        group = groups.get(current)
        current = group.parent if group else None
    return tuple(path)


def resolve_item_gst(item: StockItem, stock_groups: Mapping[str, StockGroup], on: date) -> GstRate | None:
    """GST rate of an item on a date: the item's own rate, else the nearest stock group's.

    Returns None when neither the item nor any parent group specifies GST (the
    company-level rate then applies, which callers must handle).
    """
    if item.gst.is_specified_here:
        return item.gst.rate_on(on)
    current, seen = item.parent, set()
    while current and current not in seen:
        seen.add(current)
        group = stock_groups.get(current)
        if group is None:
            return None
        if group.gst.is_specified_here:
            return group.gst.rate_on(on)
        current = group.parent
    return None
