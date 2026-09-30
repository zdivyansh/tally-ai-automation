from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tally_ai.tally.models import ImportResult


class TallyError(Exception):
    """Base class for all Tally errors."""


class TallyConnectionError(TallyError):
    """Tally could not be reached, or the request timed out."""


class TallyResponseError(TallyError):
    """Tally replied, but the reply was malformed or reported an error."""


class TallyImportError(TallyError):
    """An import (create / alter / delete) was rejected by Tally."""

    def __init__(self, result: ImportResult) -> None:
        self.result = result
        detail = "; ".join(result.line_errors) or "no error message from Tally"
        super().__init__(f"Tally import failed: {detail}")
