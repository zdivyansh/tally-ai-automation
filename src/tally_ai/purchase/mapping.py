"""Supplier item -> Tally stock item rules, kept in an Excel-friendly CSV.

One row per rule:

    keywords        words that must appear in the invoice description ("Lays", "Lays Mini Stix")
    pack            pieces per case on the invoice (e.g. 180)
    mrp             MRP per piece (e.g. 10)
    tally_item      exact Tally stock item name
    ctn_per_case    how many Tally units one invoice case is (usually 1)
    source          'you' (confirmed/edited by you) or 'learned from bill ...'
    updated         date the row was written

When several rules fit a line, the one with the most keywords wins, so
"Lays Mini Stix" beats "Lays". The file is re-read whenever it changes, so
edits made in Excel are used straight away.
"""

import csv
import os
import re
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from tally_ai.purchase.invoice import InvoiceLine

COLUMNS = ["keywords", "pack", "mrp", "tally_item", "ctn_per_case", "source", "updated"]
FILE_NAME = "pepsico_mapping.csv"


class MappingFileError(RuntimeError):
    """The mapping file is unreadable, invalid or locked (e.g. open in Excel)."""


def words(text: str) -> list[str]:
    """Lower-case words without punctuation: "Lay's STT 12.3g" -> ['lays', 'stt', '12.3g']."""
    return re.sub(r"[`'’]", "", text.lower()).replace("(", " ").replace(")", " ").split()


def leading_words(description: str, limit: int = 3) -> list[str]:
    """Product words before the first word containing a digit: 'Lays ASCO 26.5g Rs 10' -> ['Lays', 'ASCO']."""
    out: list[str] = []
    for word in description.split():
        if any(ch.isdigit() for ch in word) or len(out) == limit:
            break
        out.append(word)
    return out or description.split()[:1]


def name_numbers(name: str) -> set[Decimal]:
    """Numbers in a Tally item name: "Lay's/KK (90p) 20/-" -> {90, 20}."""
    out = set()
    for w in re.findall(r"\d+(?:\.\d+)?", name):
        out.add(Decimal(w))
    return out


@dataclass(frozen=True)
class MappingRule:
    keywords: str
    pack: int
    mrp: Decimal
    tally_item: str
    ctn_per_case: Decimal = Decimal(1)
    source: str = "you"
    updated: str = ""

    @property
    def keyword_list(self) -> list[str]:
        return words(self.keywords)

    def matches(self, line: InvoiceLine) -> bool:
        if line.pack != self.pack or line.mrp_per_piece != self.mrp:
            return False
        description = words(line.description)
        return all(k in description for k in self.keyword_list)


@dataclass(frozen=True)
class MatchOutcome:
    rule: MappingRule | None
    conflict: list[MappingRule]  # equally specific rules pointing at different items


def _decimal(value: str, column: str, row: int) -> Decimal:
    try:
        return Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        raise MappingFileError(f"row {row}: '{column}' must be a number, got {value!r}") from None


class MappingTable:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._rules: list[MappingRule] = []
        self._mtime: float | None = None

    # ------------------------------------------------------------ file
    def reload_if_changed(self) -> None:
        try:
            mtime = os.path.getmtime(self.path)
        except FileNotFoundError:
            self._rules, self._mtime = [], None
            return
        if mtime != self._mtime:
            self._rules = self._read()
            self._mtime = mtime

    def _read(self) -> list[MappingRule]:
        try:
            with open(self.path, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                missing = [
                    c for c in ("keywords", "pack", "mrp", "tally_item") if c not in (reader.fieldnames or [])
                ]
                if missing:
                    raise MappingFileError(f"{self.path.name} is missing columns: {', '.join(missing)}")
                rules = []
                for n, row in enumerate(reader, start=2):
                    if not (row.get("tally_item") or "").strip():
                        continue
                    rules.append(
                        MappingRule(
                            keywords=(row.get("keywords") or "").strip(),
                            pack=int(_decimal(row["pack"], "pack", n)),
                            mrp=Decimal(format(_decimal(row["mrp"], "mrp", n).normalize(), "f")),
                            tally_item=row["tally_item"].strip(),
                            ctn_per_case=_decimal(row.get("ctn_per_case") or "1", "ctn_per_case", n),
                            source=(row.get("source") or "").strip(),
                            updated=(row.get("updated") or "").strip(),
                        )
                    )
                return rules
        except PermissionError as e:
            raise MappingFileError(f"cannot read {self.path} (is it open in Excel?)") from e

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        try:
            with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(COLUMNS)
                for r in sorted(self._rules, key=lambda r: (r.keywords.lower(), r.pack, r.mrp)):
                    writer.writerow(
                        [r.keywords, r.pack, r.mrp, r.tally_item, r.ctn_per_case, r.source, r.updated]
                    )
            os.replace(tmp, self.path)
        except PermissionError as e:
            tmp.unlink(missing_ok=True)
            raise MappingFileError(f"cannot save {self.path.name}: close it in Excel and try again") from e
        self._mtime = os.path.getmtime(self.path)

    # ------------------------------------------------------------ rules
    @property
    def rules(self) -> list[MappingRule]:
        return list(self._rules)

    def match(self, line: InvoiceLine) -> MatchOutcome:
        fitting = [r for r in self._rules if r.matches(line)]
        if not fitting:
            return MatchOutcome(rule=None, conflict=[])
        best = max(len(r.keyword_list) for r in fitting)
        top = [r for r in fitting if len(r.keyword_list) == best]
        if len({(r.tally_item, r.ctn_per_case) for r in top}) > 1:
            return MatchOutcome(rule=None, conflict=top)
        return MatchOutcome(rule=top[0], conflict=[])

    def add(self, rule: MappingRule) -> None:
        """Add or replace the rule with the same keywords, pack and MRP."""
        stamped = replace(rule, updated=rule.updated or date.today().isoformat())
        key = (tuple(stamped.keyword_list), stamped.pack, stamped.mrp)
        self._rules = [r for r in self._rules if (tuple(r.keyword_list), r.pack, r.mrp) != key]
        self._rules.append(stamped)
