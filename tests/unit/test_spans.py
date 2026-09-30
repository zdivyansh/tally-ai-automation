import pytest

from tally_ai.agents.sales.extraction import ExtractedItem, Extraction, apply_guards
from tally_ai.agents.sales.spans import extend_to_typed, item_chunks, party_span


@pytest.mark.parametrize(
    ("message", "party"),
    [
        ("gupta store main road ko 3 ctn crunchy 300 5/- diya", "gupta store main road"),
        ("kal gupta store ko 3 ctn crunchy 300 5/- diya", "gupta store"),
        ("sold 3 ctn crunchy 300 5/- to Sharma Traders", "Sharma Traders"),
        ("bill cash sale 13 ctn mango drink 150 ml", "cash sale"),
        ("3 ctn crunchy 300 5/- verma ko", None),  # party after items: not recovered
        ("3 ctn crunchy 300 5/-", None),
    ],
)
def test_party_span(message: str, party: str | None) -> None:
    assert party_span(message) == party


def test_item_chunks_stop_at_separators() -> None:
    message = (
        "gupta ko 5 ctn masala puff 10/- @850 less 12% "
        "aur 2 ctns mix namkeen 400g, 1 carton aloo bhujia bheja"
    )
    assert item_chunks(message) == [
        (5.0, "masala puff 10/-"),
        (2.0, "mix namkeen 400g"),
        (1.0, "aloo bhujia"),
    ]


@pytest.mark.parametrize(
    ("extracted", "typed", "expected"),
    [
        ("crunchy fl & puff 10/-", "crunchy fl & puff 120 10/-", "crunchy fl & puff 120 10/-"),
        ("lay's 5/-", "lay`s 5/- 288p", "lay`s 5/- 288p"),  # pack size after the MRP kept
        ("chips 180 10/-", "chips 180 10/- cash sale", "chips 180 10/-"),  # stops at other words
        ("aloo bhujia 200g", "bik aloo bhujia 200g", "bik aloo bhujia 200g"),
        ("something else", "crunchy 300 5/-", "something else"),  # not typed: unchanged
        ("300 crunchy", "crunchy 300", "300 crunchy"),  # wrong order: unchanged
    ],
)
def test_extend_to_typed(extracted: str, typed: str, expected: str) -> None:
    assert extend_to_typed(extracted, typed) == expected


def test_guards_restore_dropped_words() -> None:
    message = "gupta store main road ko 17 ctn crunchy fl & puff 120 10/-, 16 ctn chips 270 5/- diya"
    model_output = Extraction(
        intent="sales",
        party="main road",
        items=[
            ExtractedItem(name="crunchy fl & puff 10/-", quantity=17, unit="ctn"),
            ExtractedItem(name="chips 5/-", quantity=16, unit="ctn"),
        ],
    )
    got = apply_guards(model_output, message)
    assert got.party == "gupta store main road"
    assert [i.name for i in got.items] == ["crunchy fl & puff 120 10/-", "chips 270 5/-"]


def test_guards_fill_missing_party_from_position() -> None:
    message = "cash sale ko 4 ctn chips 300 5/- bheja"
    model_output = Extraction(
        intent="sales", party=None, items=[ExtractedItem(name="chips 300 5/-", quantity=4)]
    )
    assert apply_guards(model_output, message).party == "cash sale"


def test_guards_never_replace_a_different_party() -> None:
    message = "sold 3 ctn chips 300 5/- to Sharma Traders"
    model_output = Extraction(
        intent="sales", party="Verma", items=[ExtractedItem(name="chips 300 5/-", quantity=3)]
    )
    assert apply_guards(model_output, message).party == "Verma"
