"""Evaluation case format (JSON list; see docs/evaluation.md)."""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from tally_ai.agents.sales.extraction import Intent


class ExpectedItem(BaseModel):
    name: str = Field(description="Item text as the user wrote it")
    qty: float | None = None
    unit: str | None = None
    rate: float | None = None
    discount: float | None = None


class ExpectedExtraction(BaseModel):
    """What the LLM should read from the message."""

    intent: Intent
    party: str | None = None
    items: list[ExpectedItem] | None = None
    date_text: str | None = None


class ExpectedMatch(BaseModel):
    """The Tally masters the message refers to."""

    party: str | None = None
    items: list[str] = Field(default_factory=list)


class Texts(BaseModel):
    """Exact party/item texts in the message, for matching without the LLM (--no-llm)."""

    party: str | None = None
    items: list[str] = Field(default_factory=list)
    quantities: list[float] = Field(default_factory=list)


class EvalCase(BaseModel):
    message: str
    expected: ExpectedExtraction | None = None
    expected_match: ExpectedMatch | None = None
    texts: Texts | None = None
    source: Literal["hand", "generated"] = "hand"


def load_cases(path: Path) -> list[EvalCase]:
    return [EvalCase.model_validate(c) for c in json.loads(path.read_text(encoding="utf-8"))]


def save_cases(path: Path, cases: list[EvalCase]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [c.model_dump(exclude_none=True) for c in cases]
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
