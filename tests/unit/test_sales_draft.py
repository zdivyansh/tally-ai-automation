from decimal import Decimal

import pytest

from tally_ai.agents.sales.draft import (
    AnswerError,
    QuestionKind,
    RateSource,
    SalesDraft,
    apply_answer,
    auto_resolve,
    draft_from_extraction,
    merge_correction,
    next_question,
    unit_matches,
)
from tally_ai.agents.sales.extraction import ExtractedItem, Extraction

from .agent_fixtures import TODAY, make_masters, sale

D = Decimal
M = make_masters()


def resolved(extraction: Extraction) -> SalesDraft:
    return auto_resolve(draft_from_extraction(extraction), M, TODAY)


def test_clear_names_resolve_without_questions() -> None:
    draft = resolved(sale("sharma traders", ("Crunchy 300 5/-", 3)))
    assert draft.party == "Sharma Traders"
    assert draft.lines[0].item == "Crunchy (300) 5/-"
    assert draft.date == TODAY
    # No rate in the message: always ask
    q = next_question(draft, M)
    assert q is not None and (q.kind, q.line) == (QuestionKind.RATE, 0)


def test_ambiguous_party_asks_with_choices() -> None:
    draft = resolved(sale("gupta store", ("Crunchy 300 5/-", 3)))
    q = next_question(draft, M)
    assert q is not None and q.kind is QuestionKind.PARTY_CHOICE
    assert set(q.choices[:3]) == {"Gupta Store", "Gupta Store (Main Road)", "GUPTA STORE (STATION)"}
    picked = apply_answer(draft, q, str(q.choices.index("Gupta Store (Main Road)") + 1), M, TODAY)
    assert picked.party == "Gupta Store (Main Road)"


def test_choice_answer_can_be_a_new_name() -> None:
    draft = resolved(sale("gupta store", ("Crunchy 300 5/-", 3)))
    q = next_question(draft, M)
    assert q is not None
    retyped = auto_resolve(apply_answer(draft, q, "gupta store station", M, TODAY), M, TODAY)
    assert retyped.party == "GUPTA STORE (STATION)"


def test_out_of_range_choice() -> None:
    draft = resolved(sale("gupta store", ("Crunchy 300 5/-", 3)))
    q = next_question(draft, M)
    assert q is not None
    with pytest.raises(AnswerError, match="number from 1"):
        apply_answer(draft, q, "9", M, TODAY)


def test_missing_party_asks_for_name() -> None:
    q = next_question(resolved(sale(None, ("Crunchy 300 5/-", 3))), M)
    assert q is not None and q.kind is QuestionKind.PARTY_NAME


def test_ambiguous_item_asks() -> None:
    draft = resolved(sale("sharma traders", ("crunchy 270 5", 3)))
    q = next_question(draft, M)
    assert q is not None and q.kind is QuestionKind.ITEM_CHOICE and q.line == 0
    assert set(q.choices[:2]) == {"Crunchy (270) 5/-", "Crunchy Puff (270) 5/-"}


def test_rate_answers() -> None:
    draft = resolved(sale("sharma traders", ("Crunchy 300 5/-", 3)))
    q = next_question(draft, M)
    assert q is not None
    same = apply_answer(draft, q, "same", M, TODAY).lines[0]
    assert (same.rate, same.rate_source) == (D("1310.63"), RateSource.ITEM_LAST)  # last to anyone
    customer = apply_answer(draft, q, "customer", M, TODAY).lines[0]
    assert (customer.rate, customer.rate_source) == (D(1300), RateSource.CUSTOMER_LAST)
    typed = apply_answer(draft, q, "Rs 1,295.50", M, TODAY).lines[0]
    assert (typed.rate, typed.rate_source) == (D("1295.50"), RateSource.USER)
    with pytest.raises(AnswerError):
        apply_answer(draft, q, "cheap", M, TODAY)
    with pytest.raises(AnswerError):
        apply_answer(draft, q, "0", M, TODAY)


def test_customer_rate_needs_history() -> None:
    draft = resolved(sale("verma general store", ("Crunchy 300 5/-", 3)))
    q = next_question(draft, M)
    assert q is not None
    with pytest.raises(AnswerError, match="not bought"):
        apply_answer(draft, q, "customer", M, TODAY)


def test_rate_from_message_is_not_asked() -> None:
    extraction = Extraction(
        intent="sales",
        party="sharma traders",
        items=[ExtractedItem(name="Crunchy 300 5/-", quantity=3, unit="ctn", rate=1250)],
    )
    draft = resolved(extraction)
    assert draft.lines[0].rate_source is RateSource.MESSAGE
    assert next_question(draft, M) is None


def test_missing_quantity_and_unit_mismatch() -> None:
    no_qty = resolved(sale("sharma traders", ("Crunchy 300 5/-", None)))
    q = next_question(no_qty, M)
    assert q is not None and q.kind is QuestionKind.QUANTITY

    pcs = resolved(sale("sharma traders", ("Crunchy 300 5/-", 30)))
    pcs.lines[0].unit_text = "pcs"
    q = next_question(pcs, M)
    assert q is not None and q.kind is QuestionKind.UNIT
    fixed = apply_answer(pcs, q, "2", M, TODAY).lines[0]
    assert (fixed.quantity, fixed.unit_text) == (D(2), None)


def test_unit_synonyms() -> None:
    assert unit_matches("carton", "Ctn")
    assert unit_matches("CTNS", "Ctn")
    assert unit_matches(None, "Ctn")
    assert not unit_matches("pcs", "Ctn")


def test_missing_gst_rate_asks() -> None:
    draft = resolved(sale("sharma traders", ("pure ghee 1 ltr", 1)))
    draft.lines[0].rate = D(500)
    draft.lines[0].rate_source = RateSource.USER
    q = next_question(draft, M)
    assert q is not None and q.kind is QuestionKind.GST
    assert apply_answer(draft, q, "5%", M, TODAY).lines[0].gst_pct == D(5)
    with pytest.raises(AnswerError):
        apply_answer(draft, q, "40", M, TODAY)


def test_unreadable_date_asks() -> None:
    draft = resolved(sale("sharma traders", ("Crunchy 300 5/-", 3), date_text="someday"))
    q = next_question(draft, M)
    assert q is not None and q.kind is QuestionKind.DATE
    assert apply_answer(draft, q, "25/09", M, TODAY).date is not None


def test_merge_correction_keeps_answers() -> None:
    draft = resolved(sale("gupta store", ("crunchy 270 5", 3), ("masala puff 240", 2)))
    draft.party = "GUPTA STORE (STATION)"
    draft.lines[0].item = "Crunchy Puff (270) 5/-"
    draft.lines[0].rate, draft.lines[0].rate_source = D(900), RateSource.USER
    corrected = sale("gupta store", ("crunchy 270 5", 5), ("masala puff 240", 2))
    merged = merge_correction(draft, corrected)
    assert merged.party == "GUPTA STORE (STATION)"
    assert merged.lines[0].item == "Crunchy Puff (270) 5/-"
    assert merged.lines[0].rate == D(900)
    assert merged.lines[0].quantity == D(5)
    assert merged.remote_id == draft.remote_id


def test_merge_correction_resets_changed_party() -> None:
    draft = resolved(sale("sharma traders", ("Crunchy 300 5/-", 3)))
    merged = merge_correction(draft, sale("verma general store", ("Crunchy 300 5/-", 3)))
    assert merged.party is None
    assert merged.lines[0].item == "Crunchy (300) 5/-"
