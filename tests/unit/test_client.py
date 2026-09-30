from collections.abc import Callable
from datetime import date

import httpx
import pytest

from tally_ai.tally import (
    TallyClient,
    TallyConnectionError,
    TallyImportError,
    TallyResponseError,
    VoucherKind,
)

from .sample_vouchers import sales_invoice

MockTally = Callable[..., tuple[TallyClient, list[httpx.Request]]]


def test_import_success(mock_tally: MockTally) -> None:
    client, sent = mock_tally("import_created.xml")
    result = client.import_vouchers([sales_invoice()])
    assert result.ok
    assert result.created == 1
    assert result.last_voucher_id == 12702
    assert b"<VOUCHERNUMBER>TEST/0001</VOUCHERNUMBER>" in sent[0].content
    assert sent[0].headers["content-type"].startswith("text/xml")


def test_rejected_import_raises_with_tally_message(mock_tally: MockTally) -> None:
    """Tally reports failures inside HTTP 200; the old code treated this as success."""
    client, _ = mock_tally("import_rejected.xml")
    with pytest.raises(TallyImportError, match="does not exist") as exc:
        client.import_vouchers([sales_invoice()])
    assert exc.value.result.exceptions == 1
    assert exc.value.result.created == 0


def test_rejected_import_without_check(mock_tally: MockTally) -> None:
    client, _ = mock_tally("import_rejected.xml")
    result = client.import_vouchers([sales_invoice()], check=False)
    assert not result.ok
    assert result.line_errors == ["Ledger 'Example Traders' does not exist!"]


def test_delete_by_master_id(mock_tally: MockTally) -> None:
    client, sent = mock_tally("<RESPONSE><DELETED>1</DELETED></RESPONSE>")
    assert client.delete_voucher(date(2026, 10, 1), VoucherKind.SALES, master_id=5).deleted == 1
    assert b'TAGNAME="MasterID"' in sent[0].content


def test_export_error_raises(mock_tally: MockTally) -> None:
    client, _ = mock_tally("<ENVELOPE><LINEERROR>Could not find Report</LINEERROR></ENVELOPE>")
    with pytest.raises(TallyResponseError, match="Could not find"):
        client.export_collection("Ledger", ["Name"])


def test_raw_control_characters_are_tolerated(mock_tally: MockTally) -> None:
    client, _ = mock_tally("raw_control_chars.xml")
    root = client.export_collection("StockItem", ["Name"])
    assert root.find(".//STOCKITEM") is not None


def test_unparseable_reply(mock_tally: MockTally) -> None:
    client, _ = mock_tally("<not xml")
    with pytest.raises(TallyResponseError, match="unparseable"):
        client.export_collection("Ledger", ["Name"])


def test_company_is_sent(mock_tally: MockTally) -> None:
    client, sent = mock_tally("ledgers.xml", company="Example Co")
    client.export_collection("Ledger", ["Name"])
    assert b"<SVCURRENTCOMPANY>Example Co</SVCURRENTCOMPANY>" in sent[0].content


def test_connection_error() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = TallyClient("http://tally.test:9000", transport=httpx.MockTransport(fail))
    with pytest.raises(TallyConnectionError, match="cannot reach"):
        client.send("<ENVELOPE/>")
    assert client.ping() is False


def test_timeout() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = TallyClient("http://tally.test:9000", transport=httpx.MockTransport(slow))
    with pytest.raises(TallyConnectionError, match="timed out"):
        client.send("<ENVELOPE/>")


def test_http_error_status() -> None:
    client = TallyClient(
        "http://tally.test:9000", transport=httpx.MockTransport(lambda r: httpx.Response(500))
    )
    with pytest.raises(TallyResponseError, match="HTTP 500"):
        client.send("<ENVELOPE/>")


def test_ping() -> None:
    client = TallyClient(
        "http://tally.test:9000",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, text="<RESPONSE>TallyPrime Server is Running</RESPONSE>")
        ),
    )
    assert client.ping() is True
