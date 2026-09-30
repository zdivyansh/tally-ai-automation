from decimal import Decimal
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from tally_ai.agents.sales.extraction import ExtractedItem, Extraction, LLMExtractor, apply_guards


def extraction(*items: ExtractedItem, date_text: str | None = None) -> Extraction:
    return Extraction(intent="sales", party="Sharma ji", items=list(items), date_text=date_text)


def test_mrp_is_not_a_rate() -> None:
    """Small models read '5/-' as the rate; without a rate marker it is dropped."""
    got = apply_guards(
        extraction(ExtractedItem(name="Crunchy 300", quantity=3, unit="ctn", rate=5)),
        "sold 3 ctn Crunchy 300 5/- to Sharma ji",
    )
    item = got.items[0]
    assert item.rate is None
    assert item.name == "Crunchy 300 5/-"  # MRP re-attached to the name
    assert item.quantity == 3


def test_stated_rate_and_discount_are_kept() -> None:
    got = apply_guards(
        extraction(ExtractedItem(name="Crunchy 300 5/-", quantity=50, rate=1150, discount_pct=12)),
        "Sharma ji ko 50 ctn Crunchy 300 5/- @1150 less 12% diya",
    )
    assert (got.items[0].rate, got.items[0].discount_pct) == (1150, 12)


def test_invented_numbers_are_dropped() -> None:
    got = apply_guards(
        extraction(ExtractedItem(name="Crunchy 300 5/-", quantity=7, rate=999, discount_pct=5)),
        "sold 3 ctn Crunchy 300 5/- @1150 less 12% to Sharma ji",
    )
    assert (got.items[0].quantity, got.items[0].rate, got.items[0].discount_pct) == (None, None, None)


def test_date_must_be_a_whole_word() -> None:
    message = "Kalpana store ko 2 ctn Crunchy 300 5/- diya"
    assert apply_guards(extraction(date_text="kal"), message).date_text is None
    assert apply_guards(extraction(date_text="kal"), "kal " + message).date_text == "kal"


def test_correction_keeps_earlier_values() -> None:
    previous = extraction(ExtractedItem(name="Crunchy 300 5/-", quantity=3, rate=1150, discount_pct=12))
    updated = extraction(ExtractedItem(name="Crunchy 300 5/-", quantity=5, rate=1150, discount_pct=12))
    got = apply_guards(updated, "qty 5", previous=previous)
    assert (got.items[0].quantity, got.items[0].rate, got.items[0].discount_pct) == (5, 1150, 12)


def test_correction_needs_marker_for_new_rate() -> None:
    previous = extraction(ExtractedItem(name="Crunchy 300 5/-", quantity=3))
    updated = extraction(ExtractedItem(name="Crunchy 300 5/-", quantity=3, rate=1300))
    assert apply_guards(updated, "rate 1300", previous=previous).items[0].rate == 1300
    assert apply_guards(updated, "1300", previous=previous).items[0].rate is None


class StructuredFake(FakeListChatModel):
    """FakeListChatModel whose structured output parses its JSON responses."""

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        from langchain_core.output_parsers import PydanticOutputParser

        return self | PydanticOutputParser(pydantic_object=schema)


def test_llm_extractor_applies_guards() -> None:
    reply = (
        '{"intent":"sales","party":"Sharma ji","items":[{"name":"Crunchy 300","quantity":3,'
        '"unit":"ctn","rate":5,"discount_pct":null}],"date_text":null,"narration":null}'
    )
    extractor = LLMExtractor(StructuredFake(responses=[reply]))
    got = extractor.extract("sold 3 ctn Crunchy 300 5/- to Sharma ji")
    assert got.items[0].name == "Crunchy 300 5/-"
    assert got.items[0].rate is None


@pytest.mark.parametrize(
    "phrase",
    [
        "@840",
        "rate 840",
        "rate of ctn 840",
        "ctn rate 840",
        "ctn ka rate 840",
        "rate per carton 840",
        "rate is rs 840",
        "rate: ₹840",
        "840 per ctn",
        "840/ctn",
        "per ctn 840",
        "bhav 840",
        "840 ke rate",
    ],
)
def test_rate_phrasings_are_kept(phrase: str) -> None:
    message = f"sold 3 carton chips 48 20/- to Sharma Traders {phrase} discount 5%"
    model_output = extraction(
        ExtractedItem(name="chips 48 20/-", quantity=3, unit="carton", rate=840, discount_pct=5)
    )
    got = apply_guards(model_output, message).items[0]
    assert (got.rate, got.discount_pct) == (840, 5)


def test_discount_must_be_a_percentage() -> None:
    """In 'ctn 840 discount 5%' the number before 'discount' is the rate, not a discount."""
    message = "sold 3 ctn chips 48 20/- to Sharma Traders rate of ctn 840 discount"
    model_output = extraction(ExtractedItem(name="chips 48 20/-", quantity=3, rate=840, discount_pct=840))
    got = apply_guards(model_output, message).items[0]
    assert (got.rate, got.discount_pct) == (840, None)


@pytest.mark.parametrize(
    ("phrase", "rate"), [("rate 1300", 1300), ("rate 1 ctn 840", 840), ("rate 150", 150)]
)
def test_rate_digits_are_not_split(phrase: str, rate: int) -> None:
    from tally_ai.agents.sales.extraction import _RATE_PATTERNS, _stated_values

    assert _stated_values(_RATE_PATTERNS, phrase) == {Decimal(rate)}
