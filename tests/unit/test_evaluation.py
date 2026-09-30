from datetime import date
from pathlib import Path

from tally_ai.agents.sales.extraction import ExtractedItem, Extraction
from tally_ai.evaluation.cases import EvalCase, ExpectedMatch, Texts, load_cases, save_cases
from tally_ai.evaluation.generate import generate_cases, shorthand_item, shorthand_party
from tally_ai.evaluation.runner import Outcome, evaluate_case, run_cases

from .agent_fixtures import HISTORY, TODAY, make_masters

M = make_masters()


class FixedExtractor:
    def __init__(self, extraction: Extraction) -> None:
        self.extraction = extraction

    def extract(self, message: str) -> Extraction:
        return self.extraction

    def update(self, draft: Extraction, correction: str) -> Extraction:
        raise NotImplementedError


def case(party_text: str, item_text: str, party: str, item: str) -> EvalCase:
    return EvalCase(
        message=f"{party_text} ko 3 ctn {item_text} diya",
        expected_match=ExpectedMatch(party=party, items=[item]),
        texts=Texts(party=party_text, items=[item_text], quantities=[3]),
    )


def test_outcomes_without_llm() -> None:
    clear = evaluate_case(
        case("sharma traders", "crunchy 300 5/-", "Sharma Traders", "Crunchy (300) 5/-"),
        extractor=None,
        masters=M,
        today=TODAY,
    )
    assert (clear.party, clear.items) == (Outcome.AUTO_CORRECT, [Outcome.AUTO_CORRECT])
    assert clear.ok

    asked = evaluate_case(
        case("gupta store", "crunchy 270 5", "Gupta Store (Main Road)", "Crunchy Puff (270) 5/-"),
        extractor=None,
        masters=M,
        today=TODAY,
    )
    assert (asked.party, asked.items) == (Outcome.ASKED_OFFERED, [Outcome.ASKED_OFFERED])
    assert asked.ok

    wrong = evaluate_case(
        case("sharma traders", "crunchy 300 5/-", "Verma General Store", "Crunchy (300) 5/-"),
        extractor=None,
        masters=M,
        today=TODAY,
    )
    assert wrong.party is Outcome.WRONG_AUTO
    assert not wrong.ok


def test_llm_extraction_checks_and_missing_items() -> None:
    c = EvalCase.model_validate(
        {
            "message": "sold 3 ctn crunchy 300 5/- and 2 ctn aloo bhujia 200g to sharma traders",
            "expected": {
                "intent": "sales",
                "party": "sharma traders",
                "items": [
                    {"name": "crunchy 300 5/-", "qty": 3, "unit": "ctn"},
                    {"name": "aloo bhujia 200g", "qty": 2, "unit": "ctn"},
                ],
            },
            "expected_match": {"party": "Sharma Traders", "items": ["Crunchy (300) 5/-", "Aloo Bhujia 200g"]},
        }
    )
    only_one = Extraction(
        intent="sales",
        party="sharma traders",
        items=[ExtractedItem(name="crunchy 300 5/-", quantity=3, unit="ctn")],
    )
    result = evaluate_case(c, extractor=FixedExtractor(only_one), masters=M, today=TODAY)
    assert "item count" in result.failed_checks
    assert result.items == [Outcome.AUTO_CORRECT, Outcome.NOT_EXTRACTED]
    assert not result.ok


def test_report_counts_and_render() -> None:
    cases = [
        case("sharma traders", "crunchy 300 5/-", "Sharma Traders", "Crunchy (300) 5/-"),
        case("sharma traders", "crunchy 300 5/-", "Verma General Store", "Crunchy (300) 5/-"),
    ]
    report = run_cases(cases, extractor=None, masters=M, today=TODAY)
    assert report.wrong_auto == 1
    text = report.render()
    assert "Cases: 2   fully correct: 1 (50.0%)" in text
    assert "picked WRONG without asking" in text
    assert text.index("FAIL:") > 0


def test_shorthand() -> None:
    import random

    rng = random.Random(0)
    assert shorthand_party("M/S Gupta Store (Main Road)", random.Random(1)).lower() == "gupta store main road"
    assert "(" not in shorthand_item("Crunchy (300) 5/-", rng)


def test_generated_cases_resolve_and_roundtrip(tmp_path: Path) -> None:
    lines = [*HISTORY, HISTORY[0].model_copy(update={"master_id": 3, "stock_item": "Deleted Item"})]
    cases = generate_cases(M, lines, count=10, seed=3)
    assert cases, "expected at least one case"
    for c in cases:
        assert c.source == "generated"
        assert c.expected_match is not None
        assert "Deleted Item" not in c.expected_match.items
        assert c.expected_match.party in M.ledgers
    path = tmp_path / "cases.json"
    save_cases(path, cases)
    assert load_cases(path) == cases
    report = run_cases(cases, extractor=None, masters=M, today=date(2026, 9, 30))
    assert report.wrong_auto == 0
