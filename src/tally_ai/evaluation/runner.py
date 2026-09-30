"""Run evaluation cases: did the LLM read the message, and did the agent find the right masters?"""

import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from tally_ai.agents.sales.draft import auto_resolve, draft_from_extraction
from tally_ai.agents.sales.extraction import ExtractedItem, Extraction, Extractor
from tally_ai.evaluation.cases import EvalCase, ExpectedExtraction
from tally_ai.masters.cache import MasterData
from tally_ai.masters.matcher import MatchResult, normalize


class Outcome(StrEnum):
    """How the agent handled one expected customer or item."""

    AUTO_CORRECT = "picked correctly"
    ASKED_OFFERED = "asked, correct one offered"
    ASKED_NOT_OFFERED = "asked, correct one NOT offered"
    WRONG_AUTO = "picked WRONG without asking"
    NOT_EXTRACTED = "not extracted"


GOOD = {Outcome.AUTO_CORRECT, Outcome.ASKED_OFFERED}


@dataclass
class CaseResult:
    case: EvalCase
    seconds: float
    extraction: Extraction | None = None
    error: str | None = None
    checks_passed: int = 0
    checks_total: int = 0
    failed_checks: list[str] = field(default_factory=list)
    party: Outcome | None = None
    items: list[Outcome] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        outcomes = [o for o in (self.party, *self.items) if o is not None]
        return not self.error and not self.failed_checks and all(o in GOOD for o in outcomes)


def _norm(value: object) -> str:
    """Compare like the matcher does, ignoring spaces and punctuation such as ` vs '."""
    return normalize(str(value or "")).replace(" ", "")


def _close(a: object, b: object) -> bool:
    x, y = _norm(a), _norm(b)
    return bool(x and y) and (x in y or y in x)


def check_extraction(out: Extraction, expected: ExpectedExtraction) -> list[tuple[str, bool]]:
    checks = [("intent", out.intent == expected.intent)]
    if expected.party is not None:
        checks.append(("party text", _close(out.party, expected.party)))
    if expected.items is not None:
        checks.append(("item count", len(out.items) == len(expected.items)))
        for exp, got in zip(expected.items, out.items, strict=False):
            checks.append((f"item {exp.name!r}", _close(got.name, exp.name)))
            if exp.qty is not None:
                checks.append((f"qty of {exp.name!r}", got.quantity == exp.qty))
            if exp.unit is not None:
                checks.append((f"unit of {exp.name!r}", _norm(got.unit) == _norm(exp.unit)))
            checks.append((f"rate of {exp.name!r}", got.rate == exp.rate))
            checks.append((f"discount of {exp.name!r}", got.discount_pct == exp.discount))
    if expected.date_text is not None:
        checks.append(("date text", _norm(out.date_text) == _norm(expected.date_text)))
    return checks


def _outcome(resolved: str | None, match: MatchResult, expected: str) -> Outcome:
    if resolved is not None:
        return Outcome.AUTO_CORRECT if resolved == expected else Outcome.WRONG_AUTO
    offered = {c.name for c in match.candidates}
    return Outcome.ASKED_OFFERED if expected in offered else Outcome.ASKED_NOT_OFFERED


def _from_texts(case: EvalCase) -> Extraction:
    assert case.texts is not None
    quantities = case.texts.quantities or [1.0] * len(case.texts.items)
    return Extraction(
        intent="sales",
        party=case.texts.party,
        items=[ExtractedItem(name=n, quantity=q) for n, q in zip(case.texts.items, quantities, strict=False)],
    )


def evaluate_case(
    case: EvalCase, *, extractor: Extractor | None, masters: MasterData | None, today: date
) -> CaseResult:
    start = time.perf_counter()
    try:
        extraction = extractor.extract(case.message) if extractor else _from_texts(case)
    except Exception as e:  # report and continue with the next case
        return CaseResult(case=case, seconds=time.perf_counter() - start, error=f"{type(e).__name__}: {e}")
    result = CaseResult(case=case, seconds=time.perf_counter() - start, extraction=extraction)

    if case.expected and extractor:
        checks = check_extraction(extraction, case.expected)
        result.checks_total = len(checks)
        result.checks_passed = sum(ok for _, ok in checks)
        result.failed_checks = [name for name, ok in checks if not ok]

    if case.expected_match and masters:
        draft = auto_resolve(draft_from_extraction(extraction), masters, today)
        exp = case.expected_match
        if exp.party is not None:
            if not draft.party_text:
                result.party = Outcome.NOT_EXTRACTED
            else:
                match = masters.party_matcher.match(draft.party_text)
                result.party = _outcome(draft.party, match, exp.party)
                if result.party not in GOOD:
                    result.notes.append(
                        f"party {draft.party_text!r} -> {draft.party or [c.name for c in match.candidates]}"
                    )
        for i, expected_item in enumerate(exp.items):
            if i >= len(draft.lines):
                result.items.append(Outcome.NOT_EXTRACTED)
                continue
            line = draft.lines[i]
            match = masters.item_matcher.match(line.text)
            outcome = _outcome(line.item, match, expected_item)
            result.items.append(outcome)
            if outcome not in GOOD:
                result.notes.append(
                    f"item {line.text!r} (expected {expected_item!r}) -> "
                    f"{line.item or [c.name for c in match.candidates]}"
                )
    return result


@dataclass
class Report:
    results: list[CaseResult]

    @property
    def party_outcomes(self) -> Counter[Outcome]:
        return Counter(r.party for r in self.results if r.party is not None)

    @property
    def item_outcomes(self) -> Counter[Outcome]:
        return Counter(o for r in self.results for o in r.items)

    @property
    def wrong_auto(self) -> int:
        return self.party_outcomes[Outcome.WRONG_AUTO] + self.item_outcomes[Outcome.WRONG_AUTO]

    def render(self, *, show_failures: int = 20) -> str:
        n = len(self.results)
        ok = sum(r.ok for r in self.results)
        errors = sum(r.error is not None for r in self.results)
        checks = sum(r.checks_total for r in self.results)
        passed = sum(r.checks_passed for r in self.results)
        timed = [r.seconds for r in self.results if not r.error]
        out = [f"Cases: {n}   fully correct: {ok} ({_pct(ok, n)})   errors: {errors}"]
        if checks:
            out.append(f"Extraction checks: {passed}/{checks} ({_pct(passed, checks)})")
        for label, counter in (("Customers", self.party_outcomes), ("Items", self.item_outcomes)):
            total = sum(counter.values())
            if total:
                out.append(f"{label} ({total}):")
                out.extend(f"  {o.value:<34} {counter[o]:>5}  {_pct(counter[o], total)}" for o in Outcome)
        if timed:
            out.append(f"Avg time per case: {sum(timed) / len(timed):.2f}s")
        failures = [r for r in self.results if not r.ok]
        failures.sort(key=lambda r: (Outcome.WRONG_AUTO not in (r.party, *r.items), r.error is None))
        for r in failures[:show_failures]:
            out.append(f"\nFAIL: {r.case.message}")
            if r.error:
                out.append(f"  error: {r.error}")
            if r.failed_checks:
                out.append(f"  extraction: {', '.join(r.failed_checks)}")
            out.extend(f"  {note}" for note in r.notes)
        if len(failures) > show_failures:
            out.append(f"\n... and {len(failures) - show_failures} more failures")
        return "\n".join(out)


def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "-"


def run_cases(
    cases: list[EvalCase], *, extractor: Extractor | None, masters: MasterData | None, today: date
) -> Report:
    return Report([evaluate_case(c, extractor=extractor, masters=masters, today=today) for c in cases])
