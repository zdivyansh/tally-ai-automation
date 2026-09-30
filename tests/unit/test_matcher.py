import pytest

from tally_ai.masters.matcher import Matcher, normalize

from .agent_fixtures import make_masters


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("KK  (300) 5/-", "kk 300 5 rs"),
        ("kk 300 rs 5", "kk 300 5 rs"),
        ("kk300 ₹5", "kk 300 5 rs"),
        ("Lay's (180) 10/-", "lays 180 10 rs"),
        ("Lay’s (180) 10/-", "lays 180 10 rs"),
        ("Bik. Aloo Bhujia200g", "bik aloo bhujia 200 g"),
        ("50-50 (144p) 5/-", "5050 144 p 5 rs"),
        ("1.5 kg", "1.5 kg"),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


@pytest.fixture(scope="module")
def items() -> Matcher:
    return make_masters().item_matcher


@pytest.fixture(scope="module")
def parties() -> Matcher:
    return make_masters().party_matcher


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Crunchy 300 5/-", "Crunchy (300) 5/-"),
        ("crunchy 300 5 rs", "Crunchy (300) 5/-"),
        ("crunchy 300", "Crunchy (300) 5/-"),
        ("masala puff 240", "Masala Puff (240) 5/-"),
        ("aloo bhujia 200 g", "Aloo Bhujia 200g"),
    ],
)
def test_clear_item_matches_are_confident(items: Matcher, query: str, expected: str) -> None:
    result = items.match(query)
    assert result.best is not None and result.best.name == expected
    assert result.is_confident


def test_numbers_decide_between_similar_items(items: Matcher) -> None:
    result = items.match("crunchy 270 5")
    names = [c.name for c in result.candidates[:2]]
    assert set(names) == {"Crunchy (270) 5/-", "Crunchy Puff (270) 5/-"}
    assert not result.is_confident  # two real options: ask
    assert "Crunchy (300) 5/-" not in names


def test_wrong_number_is_penalised(items: Matcher) -> None:
    candidates = {c.name: c.score for c in items.match("crunchy 300").candidates}
    assert candidates["Crunchy (300) 5/-"] - candidates.get("Crunchy (270) 5/-", 0) >= 25


def test_exact_name_with_close_variants_still_asks(parties: Matcher) -> None:
    result = parties.match("gupta store")
    assert result.best is not None and result.best.name == "Gupta Store"
    assert not result.is_confident
    assert {c.name for c in result.candidates[:3]} == {
        "Gupta Store",
        "Gupta Store (Main Road)",
        "GUPTA STORE (STATION)",
    }


def test_specific_party_is_confident(parties: Matcher) -> None:
    result = parties.match("gupta store station")
    assert result.best is not None and result.best.name == "GUPTA STORE (STATION)"
    assert result.is_confident


def test_misspelt_party(parties: Matcher) -> None:
    result = parties.match("sharma tradres")
    assert result.best is not None and result.best.name == "Sharma Traders"


def test_creditors_are_not_candidates(parties: Matcher) -> None:
    assert all(c.name != "Snacks Co Distributors" for c in parties.match("snacks co distributors").candidates)


def test_nonsense_has_no_confident_match(items: Matcher) -> None:
    assert not items.match("xyz random thing").is_confident
    assert items.match("").candidates == []


def test_aliases_are_matched() -> None:
    m = Matcher([("Crunchy (300) 5/-", ["CR300"])])
    result = m.match("cr300")
    assert result.best is not None and result.best.name == "Crunchy (300) 5/-"
    assert result.best.matched_text == "CR300"
