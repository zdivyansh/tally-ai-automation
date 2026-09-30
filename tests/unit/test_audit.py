import json
import uuid
from pathlib import Path

from tally_ai.agents.sales import SalesAgent, SalesContext
from tally_ai.audit import AuditLog

from .agent_fixtures import TODAY, make_masters, sale
from .test_sales_agent import MSG, STATION, FakeExtractor, FakeStore, choice


def agent_with_audit(store: FakeStore, audit: AuditLog) -> SalesAgent:
    extractor = FakeExtractor({MSG: sale("gupta store", ("Crunchy 300 5/-", 3))})
    return SalesAgent(
        SalesContext(
            masters=make_masters(), extractor=extractor, store=store, today=lambda: TODAY, audit=audit
        )
    )


def run_to_confirmation(agent: SalesAgent, thread: str) -> None:
    first = agent.start(MSG, thread, user="tester")
    agent.reply(choice(first.text, STATION), thread)
    agent.reply("same", thread)


def test_posted_conversation_is_fully_recorded() -> None:
    audit = AuditLog(":memory:")
    agent = agent_with_audit(FakeStore(), audit)
    thread = str(uuid.uuid4())
    run_to_confirmation(agent, thread)
    agent.reply("yes", thread)

    conv = audit.conversation(thread)
    assert conv is not None
    assert (conv["channel"], conv["user"], conv["status"]) == ("cli", "tester", "posted")
    assert conv["message"] == MSG
    assert conv["party"] == STATION
    assert conv["voucher_number"] == "TST/26-27/0008"
    assert conv["voucher_date"] == "2026-09-30"
    assert conv["total"] == "3674"
    assert conv["finished_at"] is not None
    draft = json.loads(conv["draft_json"])
    assert draft["lines"][0]["item"] == "Crunchy (300) 5/-"
    assert draft["remote_id"] == conv["remote_id"]

    events = audit.events(thread)
    assert [e["role"] for e in events] == ["user", "agent", "user", "agent", "user", "agent", "user", "agent"]
    assert events[0]["text"] == MSG
    assert events[-1]["text"].startswith("Posted sales invoice TST/26-27/0008")

    (posting,) = audit.postings(thread)
    assert posting["ok"] == 1
    assert posting["voucher_number"] == "TST/26-27/0008"
    assert "<VOUCHERNUMBER>TST/26-27/0008</VOUCHERNUMBER>" in posting["request_xml"]
    assert json.loads(posting["result_json"])["created"] == 1


def test_rejected_posting_is_recorded() -> None:
    audit = AuditLog(":memory:")
    agent = agent_with_audit(FakeStore(reject="Ledger 'X' does not exist!"), audit)
    thread = str(uuid.uuid4())
    run_to_confirmation(agent, thread)
    agent.reply("yes", thread)

    conv = audit.conversation(thread)
    assert conv is not None and conv["status"] == "failed"
    assert conv["voucher_number"] is None
    (posting,) = audit.postings(thread)
    assert posting["ok"] == 0
    assert "does not exist" in posting["error"]
    assert json.loads(posting["result_json"])["line_errors"] == ["Ledger 'X' does not exist!"]


def test_cancelled_conversation_has_no_posting() -> None:
    audit = AuditLog(":memory:")
    agent = agent_with_audit(FakeStore(), audit)
    thread = str(uuid.uuid4())
    agent.start(MSG, thread)
    agent.reply("cancel", thread)
    conv = audit.conversation(thread)
    assert conv is not None and conv["status"] == "cancelled"
    assert audit.postings(thread) == []


def test_file_database_persists(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "audit.db"
    audit = AuditLog(path)
    audit.start("c1", channel="cli", user=None, message="hello")
    audit.close()
    reopened = AuditLog(path)
    conv = reopened.conversation("c1")
    assert conv is not None and conv["message"] == "hello"
    reopened.close()
