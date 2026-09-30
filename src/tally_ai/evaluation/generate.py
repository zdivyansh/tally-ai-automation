"""Generate evaluation cases from past sales invoices.

Each case is a message written the way people type in chat ("radhe shyam ko
3 ctn kk 300 5/- diya") about a real past invoice, so the correct customer and
items are known. Output contains real customer names: keep it local.
"""

import random
import re
from collections import defaultdict
from collections.abc import Iterable

from tally_ai.evaluation.cases import EvalCase, ExpectedExtraction, ExpectedItem, ExpectedMatch, Texts
from tally_ai.masters.cache import DEBTORS, MasterData
from tally_ai.tally.masters import SalesLine

TEMPLATES_ONE = [
    "sold {q1} {u1} {i1} to {p}",
    "{p} ko {q1} {u1} {i1} diya",
    "{p} ko {q1} {u1} {i1} bheja",
    "{q1} {u1} {i1} {p} ko",
    "bill {p} {q1} {u1} {i1}",
]
TEMPLATES_TWO = [
    "sold {q1} {u1} {i1} and {q2} {u2} {i2} to {p}",
    "{p} ko {q1} {u1} {i1} aur {q2} {u2} {i2} bheja",
    "{p} ko {q1} {u1} {i1}, {q2} {u2} {i2} diya",
]


def shorthand_party(name: str, rng: random.Random) -> str:
    """How a customer name is typed in chat: lower case, no brackets or punctuation."""
    text = re.sub(r"^m/s\.?\s*", "", name.strip(), flags=re.I)
    text = re.sub(r"[().,&]", " ", text)
    text = " ".join(text.split())
    return text.lower() if rng.random() < 0.8 else text


def shorthand_item(name: str, rng: random.Random) -> str:
    """How an item is typed: lower case, brackets dropped, MRP kept ('5/-' or '5rs')."""
    text = re.sub(r"[()]", " ", name)
    text = re.sub(r"(?<=[a-z])\.(?=\s|$)", "", text, flags=re.I)  # 'Bik.' -> 'Bik'
    text = " ".join(text.split()).rstrip(".")
    if rng.random() < 0.2:
        text = re.sub(r"(\d+)\s*/-", r"\1rs", text)
    return text.lower() if rng.random() < 0.85 else text


def generate_cases(
    masters: MasterData, lines: Iterable[SalesLine], *, count: int, seed: int = 1
) -> list[EvalCase]:
    """Up to `count` cases (1 or 2 items each) from vouchers whose party and items still exist."""
    rng = random.Random(seed)
    by_voucher: dict[int, list[SalesLine]] = defaultdict(list)
    for line in lines:
        by_voucher[line.master_id].append(line)

    vouchers = []
    for voucher_lines in by_voucher.values():
        party = masters.ledgers.get(voucher_lines[0].party)
        items = list(
            dict.fromkeys(line.stock_item for line in voucher_lines if line.stock_item in masters.items)
        )
        if party and party.is_under(DEBTORS) and items:
            vouchers.append((party.name, items))
    rng.shuffle(vouchers)

    cases: list[EvalCase] = []
    seen: set[str] = set()
    for party_name, items in vouchers:
        if len(cases) >= count:
            break
        chosen = rng.sample(items, k=min(len(items), rng.choice([1, 1, 2])))
        party_text = shorthand_party(party_name, rng)
        item_texts = [shorthand_item(item, rng) for item in chosen]
        quantities = [float(rng.randint(1, 25)) for _ in chosen]
        units = [(masters.items[item].base_unit or "").lower() for item in chosen]
        fields: dict[str, object] = {"p": party_text}
        for n, (text, qty, unit) in enumerate(zip(item_texts, quantities, units, strict=True), 1):
            fields |= {f"i{n}": text, f"q{n}": int(qty), f"u{n}": unit}
        template = rng.choice(TEMPLATES_ONE if len(chosen) == 1 else TEMPLATES_TWO)
        message = " ".join(template.format(**fields).split())
        if message in seen:
            continue
        seen.add(message)
        cases.append(
            EvalCase(
                message=message,
                source="generated",
                expected=ExpectedExtraction(
                    intent="sales",
                    party=party_text,
                    items=[
                        ExpectedItem(name=text, qty=qty, unit=unit or None)
                        for text, qty, unit in zip(item_texts, quantities, units, strict=True)
                    ],
                ),
                expected_match=ExpectedMatch(party=party_name, items=chosen),
                texts=Texts(party=party_text, items=item_texts, quantities=quantities),
            )
        )
    return cases
