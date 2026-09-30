"""The agent's view of Tally: existing voucher numbers, posting, and read-back."""

from datetime import date
from typing import Protocol

from tally_ai.accounting.dates import fy_end, fy_start
from tally_ai.tally.client import TallyClient
from tally_ai.tally.masters import VoucherSummary
from tally_ai.tally.models import ImportResult, Voucher
from tally_ai.tally.queries import TallyQueries


class VoucherStore(Protocol):
    def numbers_around(self, voucher_type: str, day: date) -> list[str | None]:
        """Voucher numbers of this type in the day's financial year and the previous one."""
        ...

    def post(self, voucher: Voucher) -> ImportResult: ...

    def find(self, voucher_type: str, number: str, day: date) -> list[VoucherSummary]: ...


class TallyVoucherStore:
    def __init__(self, client: TallyClient) -> None:
        self.client = client
        self.queries = TallyQueries(client)

    def numbers_around(self, voucher_type: str, day: date) -> list[str | None]:
        start = fy_start(date(fy_start(day).year - 1, 4, 1))
        return [
            v.number
            for v in self.queries.vouchers(voucher_type=voucher_type, from_date=start, to_date=fy_end(day))
        ]

    def post(self, voucher: Voucher) -> ImportResult:
        return self.client.import_vouchers([voucher])

    def find(self, voucher_type: str, number: str, day: date) -> list[VoucherSummary]:
        return self.queries.vouchers(voucher_type=voucher_type, number=number, from_date=day, to_date=day)
