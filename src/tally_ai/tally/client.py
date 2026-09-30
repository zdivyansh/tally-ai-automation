"""Low-level HTTP client for Tally's XML interface."""

import logging
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from datetime import date
from types import TracebackType
from typing import Self

import httpx

from tally_ai.config import Settings
from tally_ai.tally import xml_builder
from tally_ai.tally.errors import TallyConnectionError, TallyImportError, TallyResponseError
from tally_ai.tally.models import ImportResult, Voucher, VoucherKind
from tally_ai.tally.parsing import sanitize_xml, text

logger = logging.getLogger(__name__)


def parse_import_result(root: ET.Element) -> ImportResult:
    def count(tag: str) -> int:
        value = text(root, f".//{tag}")
        try:
            return int(value) if value else 0
        except ValueError:
            return 0

    line_errors = [e.text.strip() for e in root.iter("LINEERROR") if e.text and e.text.strip()]
    last_id = count("LASTVCHID")
    return ImportResult(
        created=count("CREATED"),
        altered=count("ALTERED"),
        deleted=count("DELETED"),
        combined=count("COMBINED"),
        ignored=count("IGNORED"),
        cancelled=count("CANCELLED"),
        errors=count("ERRORS"),
        exceptions=count("EXCEPTIONS"),
        last_voucher_id=last_id or None,
        line_errors=line_errors,
    )


class TallyClient:
    """Sends XML to Tally and returns parsed responses.

    Unlike `requests`-style wrappers, Tally reports most failures inside an
    HTTP 200 reply, so every response is checked for LINEERROR / ERRORS.
    """

    def __init__(
        self,
        url: str,
        *,
        company: str | None = None,
        timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = url
        self.company = company
        self._http = httpx.Client(timeout=timeout, transport=transport)

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(settings.tally_url, company=settings.tally_company, timeout=settings.tally_timeout)

    # ------------------------------------------------------------ lifecycle
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    # ------------------------------------------------------------ transport
    def ping(self) -> bool:
        """True if a Tally server answers at the URL."""
        try:
            response = self._http.get(self.url)
        except httpx.HTTPError:
            return False
        return response.status_code == 200 and "running" in response.text.lower()

    def send(self, xml: str) -> str:
        """POST raw XML and return the sanitized response text."""
        try:
            response = self._http.post(
                self.url,
                content=xml.encode("utf-8"),
                headers={"Content-Type": "text/xml; charset=utf-8"},
            )
        except httpx.TimeoutException as e:
            raise TallyConnectionError(f"Tally at {self.url} timed out") from e
        except httpx.HTTPError as e:
            raise TallyConnectionError(f"cannot reach Tally at {self.url}: {e}") from e
        if response.status_code != 200:
            raise TallyResponseError(f"Tally returned HTTP {response.status_code}")
        return sanitize_xml(response.text)

    def request(self, xml: str) -> ET.Element:
        raw = self.send(xml)
        try:
            return ET.fromstring(raw)
        except ET.ParseError as e:
            raise TallyResponseError(f"unparseable reply from Tally: {e}") from e

    # ------------------------------------------------------------ export
    def export_collection(
        self,
        object_type: str,
        fetch: Sequence[str],
        *,
        filters: Sequence[str] = (),
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> ET.Element:
        root = self.request(
            xml_builder.build_collection_request(
                object_type,
                fetch,
                filters=filters,
                from_date=from_date,
                to_date=to_date,
                company=self.company,
            )
        )
        errors = [e.text.strip() for e in root.iter("LINEERROR") if e.text and e.text.strip()]
        if errors:
            raise TallyResponseError(f"Tally export of {object_type} failed: {'; '.join(errors)}")
        return root

    # ------------------------------------------------------------ import
    def _import(self, xml: str, *, check: bool) -> ImportResult:
        result = parse_import_result(self.request(xml))
        logger.debug("import result: %s", result)
        if check and not result.ok:
            raise TallyImportError(result)
        return result

    def import_vouchers(self, vouchers: Sequence[Voucher], *, check: bool = True) -> ImportResult:
        """Create (or, with remote_id, create-or-alter) vouchers.

        Raises TallyImportError when Tally rejects anything, unless check=False.
        """
        return self._import(xml_builder.build_import_request(vouchers, self.company), check=check)

    def delete_voucher(
        self,
        voucher_date: date,
        kind: VoucherKind | str,
        *,
        number: str | None = None,
        master_id: int | None = None,
        check: bool = True,
    ) -> ImportResult:
        xml = xml_builder.build_delete_voucher_request(
            voucher_date, kind, number=number, master_id=master_id, company=self.company
        )
        return self._import(xml, check=check)
