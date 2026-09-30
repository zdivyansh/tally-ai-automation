"""End-to-end: real LLM + real Tally, full conversation, then clean up.

    TALLY_LIVE=1 TALLY_TEST_DATE=2026-10-01 \
    TALLY_TEST_MESSAGE="sold 3 ctn <item> to <customer>" \
    uv run pytest tests/live/test_sales_agent_live.py -v -s

Questions are answered automatically: numbered choices with '1', rates with
'same'. The voucher is numbered TEST/CLAUDE/<yy-yy>/<nnnn> (not the real
series) and deleted afterwards.
"""

import os
import re
import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest

from tally_ai.agents.sales import SalesAgent, SalesContext
from tally_ai.agents.sales.extraction import LLMExtractor
from tally_ai.agents.sales.store import TallyVoucherStore
from tally_ai.config import Settings
from tally_ai.llm import create_chat_model
from tally_ai.masters.cache import MasterData
from tally_ai.tally import TallyClient, TallyQueries

pytestmark = pytest.mark.live

PREFIX = "TEST/CLAUDE"
TODAY = date.fromisoformat(os.environ["TALLY_TEST_DATE"]) if os.getenv("TALLY_TEST_DATE") else date.today()
MESSAGE = os.getenv("TALLY_TEST_MESSAGE")


@pytest.fixture(scope="module")
def client() -> Iterator[TallyClient]:
    with TallyClient.from_settings(Settings()) as c:
        yield c


def auto_answer(text: str) -> str:
    if "Reply 'yes' to post" in text:
        return "yes"
    if re.search(r"^\s*1\. ", text, re.M):
        return "1"
    if text.startswith("Rate for") or "\nRate for" in text:
        return "same" if "'same'" in text else "100"
    if "What GST %" in text:
        return "5"
    if "How many" in text:
        return "1"
    raise AssertionError(f"unexpected question:\n{text}")


@pytest.mark.skipif(not MESSAGE, reason="set TALLY_TEST_MESSAGE")
def test_message_to_posted_voucher(client: TallyClient) -> None:
    settings = Settings()
    queries = TallyQueries(client)
    masters = MasterData.load(queries, settings, TODAY)
    store = TallyVoucherStore(client)
    agent = SalesAgent(
        SalesContext(
            masters=masters,
            extractor=LLMExtractor(create_chat_model(settings)),
            store=store,
            voucher_type=settings.sales_voucher_type,
            number_prefix=PREFIX,
            today=lambda: TODAY,
        )
    )
    thread = str(uuid.uuid4())
    turn = agent.start(MESSAGE or "", thread)
    transcript = [f"user: {MESSAGE}"]
    summary = ""
    while not turn.done:
        transcript.append(f"agent: {turn.text}")
        if "Reply 'yes' to post" in turn.text:
            summary = turn.text
        answer = auto_answer(turn.text)
        transcript.append(f"user: {answer}")
        turn = agent.reply(answer, thread)
    transcript.append(f"agent: {turn.text}")
    print("\n\n".join(transcript))

    assert turn.status == "posted", turn.text
    number = re.search(r"invoice (\S+)", turn.text)
    assert number is not None
    posted = number.group(1)
    assert posted.startswith(f"{PREFIX}/")
    try:
        found = queries.vouchers(
            voucher_type=settings.sales_voucher_type, number=posted, from_date=TODAY, to_date=TODAY
        )
        assert len(found) == 1
        # Tally's stored party amount equals the total shown to the user
        total = re.search(r"Total\s+₹([\d,]+\.\d\d)", summary)
        assert total is not None
        root = client.export_collection(
            "Voucher",
            ["VoucherNumber", "LedgerEntries.LedgerName", "LedgerEntries.Amount"],
            filters=[f'$VoucherNumber = "{posted}"'],
            from_date=TODAY,
            to_date=TODAY,
        )
        party_amount = next(
            Decimal((e.findtext("AMOUNT") or "0").strip())
            for e in root.iter("LEDGERENTRIES.LIST")
            if (e.findtext("LEDGERNAME") or "").strip() == found[0].party_ledger
        )
        assert -party_amount == Decimal(total.group(1).replace(",", ""))
    finally:
        for v in queries.vouchers(
            voucher_type=settings.sales_voucher_type, number=posted, from_date=TODAY, to_date=TODAY
        ):
            if v.master_id:
                client.delete_voucher(v.date, settings.sales_voucher_type, master_id=v.master_id)
        assert queries.vouchers(number=posted, from_date=TODAY, to_date=TODAY) == []
