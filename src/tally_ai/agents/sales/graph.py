"""LangGraph pipeline for sales vouchers.

    extract -> resolve <-> ask -> build -> confirm -> post
                  ^                           |
                  +------- correction --------+

The LLM runs only in `extract` and when applying a correction. `ask` and
`confirm` pause the graph with `interrupt()`; the caller resumes it with the
user's reply. State is checkpointed per conversation thread.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from tally_ai.accounting.numbering import NumberingError, derive_prefix, next_number
from tally_ai.accounting.sales_invoice import InvoiceError
from tally_ai.agents.sales.compose import ComposedInvoice, compose_invoice
from tally_ai.agents.sales.draft import (
    AnswerError,
    SalesDraft,
    apply_answer,
    auto_resolve,
    draft_from_extraction,
    merge_correction,
    next_question,
)
from tally_ai.agents.sales.extraction import Extractor
from tally_ai.agents.sales.render import inr, render_invoice, render_question
from tally_ai.agents.sales.store import VoucherStore
from tally_ai.audit import AuditLog
from tally_ai.masters.cache import MasterData
from tally_ai.tally.errors import TallyError
from tally_ai.tally.masters import SalesLine
from tally_ai.tally.xml_builder import build_import_request

logger = logging.getLogger(__name__)

Status = Literal["running", "posted", "cancelled", "failed", "unsupported"]

YES = {
    "yes",
    "yup",
    "y",
    "ok",
    "okay",
    "haan",
    "han",
    "ha",
    "haa",
    "post",
    "confirm",
    "done",
    "theek hai",
    "thik hai",
    "thik",
    "theek",
}
NO = {"no", "n", "nahi", "nahin", "cancel", "stop"}
CANCEL = {"cancel", "stop", "exit", "quit"}


class SalesState(TypedDict, total=False):
    message: str
    draft: SalesDraft
    notice: str | None
    number: str | None
    summary: str | None
    status: Status
    result: str | None
    thread_id: str
    total: str | None


@dataclass
class SalesContext:
    masters: MasterData
    extractor: Extractor
    store: VoucherStore
    voucher_type: str = "Sales"
    number_prefix: str | None = None
    today: Callable[[], date] = date.today
    audit: AuditLog | None = None
    channel: str = "cli"


def _normalize_reply(text: str) -> str:
    return " ".join(text.strip().lower().strip(".!").split())


# Types stored in checkpoints; LangGraph only deserializes explicitly allowed classes
CHECKPOINT_TYPES = [
    ("tally_ai.agents.sales.draft", "SalesDraft"),
    ("tally_ai.agents.sales.draft", "LineDraft"),
    ("tally_ai.agents.sales.draft", "RateSource"),
    ("tally_ai.agents.sales.extraction", "Extraction"),
    ("tally_ai.agents.sales.extraction", "ExtractedItem"),
]


def default_checkpointer() -> InMemorySaver:
    return InMemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES))


def build_graph(ctx: SalesContext, checkpointer: BaseCheckpointSaver[Any] | None = None) -> Any:
    masters = ctx.masters

    def compute_number(day: date) -> str:
        existing = ctx.store.numbers_around(ctx.voucher_type, day)
        prefix = ctx.number_prefix or derive_prefix(existing)
        return next_number(prefix, day, existing)

    def compose(draft: SalesDraft, number: str) -> ComposedInvoice:
        return compose_invoice(
            draft, masters, number=number, today=ctx.today(), voucher_type=ctx.voucher_type
        )

    # ------------------------------------------------------------ nodes
    def extract(state: SalesState) -> dict[str, Any]:
        try:
            extraction = ctx.extractor.extract(state["message"])
        except Exception as e:  # LLM providers raise many types
            logger.exception("extraction failed")
            return {"status": "failed", "result": f"Could not read the message ({type(e).__name__}: {e})."}
        if extraction.intent != "sales":
            return {
                "status": "unsupported",
                "result": f"This looks like a {extraction.intent} entry. "
                "Only sales invoices are supported for now.",
            }
        if not extraction.items:
            return {"status": "failed", "result": "I could not find any items in the message."}
        return {"draft": draft_from_extraction(extraction), "status": "running", "notice": None}

    def resolve(state: SalesState) -> dict[str, Any]:
        return {"draft": auto_resolve(state["draft"], masters, ctx.today())}

    def ask(state: SalesState) -> dict[str, Any]:
        draft = state["draft"]
        question = next_question(draft, masters)
        assert question is not None
        text = render_question(question, draft, masters)
        if state.get("notice"):
            text = f"{state['notice']}\n{text}"
        reply = str(interrupt({"kind": "question", "text": text}))
        if _normalize_reply(reply) in CANCEL:
            return {"status": "cancelled", "result": "Cancelled. Nothing was posted."}
        try:
            return {"draft": apply_answer(draft, question, reply, masters, ctx.today()), "notice": None}
        except AnswerError as e:
            return {"notice": str(e)}

    def build(state: SalesState) -> dict[str, Any]:
        draft = state["draft"]
        assert draft.date is not None
        try:
            number = compute_number(draft.date)
            composed = compose(draft, number)
        except (NumberingError, InvoiceError, TallyError) as e:
            return {"status": "failed", "result": f"Cannot build the invoice: {e}"}
        return {"number": number, "summary": render_invoice(composed, draft, masters)}

    def confirm(state: SalesState) -> dict[str, Any]:
        reply = str(interrupt({"kind": "confirm", "text": state["summary"]}))
        word = _normalize_reply(reply)
        if word in YES:
            return {"status": "running"}
        if word in NO:
            return {"status": "cancelled", "result": "Cancelled. Nothing was posted."}
        draft = state["draft"]
        try:
            corrected = ctx.extractor.update(draft.extraction, reply)
        except Exception as e:
            logger.exception("correction failed")
            return {"notice": f"Could not apply that correction ({type(e).__name__}). Please try again."}
        if not corrected.items:
            return {"notice": "That correction removed every item; please rephrase."}
        return {"draft": merge_correction(draft, corrected), "summary": None, "notice": None}

    def post(state: SalesState) -> dict[str, Any]:
        draft = state["draft"]
        assert draft.date is not None and draft.party is not None
        shown = state["number"]
        try:
            # Someone may have billed in Tally meanwhile: take the number again
            number = compute_number(draft.date)
            composed = compose(draft, number)
        except (NumberingError, InvoiceError, TallyError) as e:
            return {"status": "failed", "result": f"Cannot build the invoice: {e}"}
        voucher = composed.invoice.voucher

        def audit_posting(ok: bool, result: dict[str, Any] | None, error: str | None) -> None:
            if ctx.audit and state.get("thread_id"):
                ctx.audit.posting(
                    state["thread_id"],
                    request_xml=build_import_request([voucher]),
                    ok=ok,
                    voucher_number=number,
                    voucher_date=voucher.date.isoformat(),
                    party=voucher.party_ledger,
                    total=str(composed.invoice.total),
                    remote_id=voucher.remote_id,
                    result=result,
                    error=error,
                )

        try:
            result = ctx.store.post(voucher)
        except TallyError as e:
            import_result = getattr(e, "result", None)
            audit_posting(False, import_result.model_dump() if import_result else None, str(e))
            return {"status": "failed", "result": f"Tally did not accept the invoice: {e}"}
        audit_posting(True, result.model_dump(), None)
        try:
            found = ctx.store.find(ctx.voucher_type, number, draft.date)
        except TallyError:
            logger.exception("posted %s but could not read it back", number)
            found = []

        # Later rate hints and discounts should see this invoice
        master_id = found[0].master_id if found and found[0].master_id else 0
        for inv in composed.invoice.voucher.inventory:
            masters.history.add(
                SalesLine(
                    date=draft.date,
                    master_id=master_id,
                    number=number,
                    party=draft.party,
                    stock_item=inv.stock_item,
                    rate=inv.rate,
                    unit=inv.unit,
                    discount_pct=inv.discount_pct,
                )
            )
        note = f" (number changed from {shown}: another voucher took it)" if shown and shown != number else ""
        verb = "Updated" if result.altered and not result.created else "Posted"
        return {
            "status": "posted",
            "number": number,
            "total": str(composed.invoice.total),
            "result": f"{verb} sales invoice {number} for {draft.party}, "
            f"total {inr(composed.invoice.total)}{note}.",
        }

    # ------------------------------------------------------------ routing
    def after_extract(state: SalesState) -> str:
        return "resolve" if state.get("status") == "running" else END

    def after_resolve(state: SalesState) -> str:
        return "ask" if next_question(state["draft"], masters) else "build"

    def after_ask(state: SalesState) -> str:
        return END if state.get("status") == "cancelled" else "resolve"

    def after_build(state: SalesState) -> str:
        return END if state.get("status") == "failed" else "confirm"

    def after_confirm(state: SalesState) -> str:
        if state.get("status") == "cancelled":
            return END
        if state.get("summary") is None:
            return "resolve"  # correction applied
        if state.get("notice"):
            return "confirm_again"
        return "post"

    def confirm_again(state: SalesState) -> dict[str, Any]:
        summary = state["summary"] or ""
        return {"summary": f"{state['notice']}\n\n{summary}", "notice": None}

    graph = StateGraph(SalesState)
    graph.add_node("extract", extract)
    graph.add_node("resolve", resolve)
    graph.add_node("ask", ask)
    graph.add_node("build", build)
    graph.add_node("confirm", confirm)
    graph.add_node("confirm_again", confirm_again)
    graph.add_node("post", post)
    graph.add_edge(START, "extract")
    graph.add_conditional_edges("extract", after_extract, ["resolve", END])
    graph.add_conditional_edges("resolve", after_resolve, ["ask", "build"])
    graph.add_conditional_edges("ask", after_ask, ["resolve", END])
    graph.add_conditional_edges("build", after_build, ["confirm", END])
    graph.add_conditional_edges("confirm", after_confirm, ["resolve", "confirm_again", "post", END])
    graph.add_edge("confirm_again", "confirm")
    graph.add_edge("post", END)
    return graph.compile(checkpointer=checkpointer or default_checkpointer())


@dataclass(frozen=True)
class AgentTurn:
    text: str
    done: bool
    status: Status


class SalesAgent:
    """Drives the graph one user message at a time, recording everything in the audit log."""

    def __init__(self, ctx: SalesContext, checkpointer: BaseCheckpointSaver[Any] | None = None) -> None:
        self.ctx = ctx
        self.graph = build_graph(ctx, checkpointer)

    @staticmethod
    def _config(thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}}

    def _run(self, payload: Any, thread_id: str) -> AgentTurn:
        state = self.graph.invoke(payload, self._config(thread_id))
        interrupts = state.get("__interrupt__")
        if interrupts:
            turn = AgentTurn(text=str(interrupts[0].value["text"]), done=False, status="running")
        else:
            turn = AgentTurn(text=state.get("result") or "", done=True, status=state.get("status", "failed"))
        audit = self.ctx.audit
        if audit:
            audit.event(thread_id, "agent", turn.text)
            if turn.done:
                self._finish(audit, thread_id, turn)
        return turn

    def _finish(self, audit: AuditLog, thread_id: str, turn: AgentTurn) -> None:
        values = self.graph.get_state(self._config(thread_id)).values
        draft: SalesDraft | None = values.get("draft")
        audit.finish(
            thread_id,
            status=turn.status,
            result=turn.text,
            draft=draft.model_dump(mode="json") if draft else None,
            party=draft.party if draft else None,
            voucher_date=draft.date.isoformat() if draft and draft.date else None,
            voucher_number=values.get("number") if turn.status == "posted" else None,
            total=values.get("total"),
            remote_id=draft.remote_id if draft else None,
        )

    def start(self, message: str, thread_id: str, *, user: str | None = None) -> AgentTurn:
        if self.ctx.audit:
            self.ctx.audit.start(thread_id, channel=self.ctx.channel, user=user, message=message)
        return self._run({"message": message, "thread_id": thread_id}, thread_id)

    def reply(self, answer: str, thread_id: str) -> AgentTurn:
        if self.ctx.audit:
            self.ctx.audit.event(thread_id, "user", answer)
        return self._run(Command(resume=answer), thread_id)
