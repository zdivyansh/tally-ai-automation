"""Look-ups over past sales lines: last rates and discounts."""

from collections.abc import Iterable
from decimal import Decimal

from tally_ai.tally.masters import SalesLine


class SalesHistory:
    """Index of past sales lines. Later lines win (lines must be passed oldest first)."""

    def __init__(self, lines: Iterable[SalesLine]) -> None:
        self._item: dict[str, SalesLine] = {}
        self._customer_item: dict[tuple[str, str], SalesLine] = {}
        for line in lines:
            self.add(line)

    def add(self, line: SalesLine) -> None:
        self._item[line.stock_item] = line
        self._customer_item[(line.party, line.stock_item)] = line

    def last_for_item(self, item: str) -> SalesLine | None:
        return self._item.get(item)

    def last_for_customer(self, party: str, item: str) -> SalesLine | None:
        return self._customer_item.get((party, item))

    def customer_discount(self, party: str, item: str) -> Decimal:
        """This customer's last discount on the item; 0 if they never bought it."""
        line = self.last_for_customer(party, item)
        return line.discount_pct if line else Decimal(0)

    def items_bought_by(self, party: str) -> set[str]:
        return {item for (p, item) in self._customer_item if p == party}
