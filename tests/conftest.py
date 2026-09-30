import os
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from tally_ai.tally import TallyClient

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.getenv("TALLY_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live Tally test; set TALLY_LIVE=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def mock_tally() -> Callable[..., tuple[TallyClient, list[httpx.Request]]]:
    """Build a TallyClient whose HTTP replies come from fixture files, in order."""

    def make(*replies: str, company: str | None = None) -> tuple[TallyClient, list[httpx.Request]]:
        sent: list[httpx.Request] = []
        queue = list(replies)

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            body = queue.pop(0)
            return httpx.Response(200, text=fixture_text(body) if body.endswith(".xml") else body)

        client = TallyClient(
            "http://tally.test:9000", company=company, transport=httpx.MockTransport(handler)
        )
        return client, sent

    return make
