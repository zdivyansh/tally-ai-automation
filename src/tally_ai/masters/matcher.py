"""Fuzzy matching of user-typed names to Tally master names.

Numbers carry most of the meaning in item names ("KK (300) 5/-" vs
"KK (270) 5/-"), so every number the user typed must appear in the candidate;
otherwise the candidate is heavily penalised.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from rapidfuzz import fuzz

AUTO_ACCEPT_SCORE = 88.0  # best candidate must score at least this ...
AUTO_ACCEPT_MARGIN = 8.0  # ... and beat the runner-up by this much
MIN_CANDIDATE_SCORE = 45.0
MISSING_NUMBER_PENALTY = 30.0
EXTRA_NUMBER_PENALTY = 2.0

_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def normalize(text: str) -> str:
    """Canonical form for comparison.

    'KK  (300) 5/-' -> 'kk 300 5 rs'; "Lay's" -> 'lays'; '200g' -> '200 g'; '50-50' -> '5050'.
    """
    t = re.sub(r"['’]", "", text.lower().replace("&", " and "))  # Lay's / Lay’s -> lays
    t = re.sub(r"(\d)\s*/-", r"\1 rs ", t)  # MRP marker: 5/- == 5 rs
    t = re.sub(r"(?:₹|\brs\b\.?|\brupees?\b|\brupaye\b)\s*(\d+)", r"\1 rs ", t)  # Rs.5 / Rs 5 == 5 rs
    t = re.sub(r"(\d)-(\d)", r"\1\2", t)  # 50-50 -> 5050
    t = re.sub(r"(\d)([a-z])", r"\1 \2", t)
    t = re.sub(r"([a-z])(\d)", r"\1 \2", t)
    t = re.sub(r"[^a-z0-9+.\s]", " ", t)
    t = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", t)  # keep decimal points only
    return " ".join(t.split())


def numbers(text: str) -> list[str]:
    return [str(float(n)).rstrip("0").rstrip(".") for n in _NUMBER.findall(text)]


@dataclass(frozen=True)
class Candidate:
    name: str
    score: float
    matched_text: str  # the name or alias that produced the score


@dataclass(frozen=True)
class MatchResult:
    query: str
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None

    @property
    def is_confident(self) -> bool:
        """A single clear winner: high score and a clear margin over the runner-up.

        An exact name is not enough on its own: 'gupta store' must still ask when
        'Gupta Store (Main Road)' scores almost as high.
        """
        best = self.best
        if best is None:
            return False
        runner_up = self.candidates[1].score if len(self.candidates) > 1 else 0.0
        return best.score >= AUTO_ACCEPT_SCORE and best.score - runner_up >= AUTO_ACCEPT_MARGIN


@dataclass(frozen=True)
class _Entry:
    name: str
    text: str
    normalized: str
    numbers: tuple[str, ...]


class Matcher:
    """Matches queries against a fixed set of names (with optional aliases)."""

    def __init__(self, names: Iterable[tuple[str, Iterable[str]]]) -> None:
        self._entries = [
            _Entry(name=name, text=text, normalized=normalize(text), numbers=tuple(numbers(normalize(text))))
            for name, aliases in names
            for text in (name, *aliases)
        ]

    @staticmethod
    def score(
        query_norm: str, query_numbers: list[str], entry_norm: str, entry_numbers: tuple[str, ...]
    ) -> float:
        # token_set_ratio is 100 when one side's words are a subset of the other's. That is right
        # for a short query ('crunchy 300' -> 'Crunchy (300) 5/-') but wrong when the query has
        # extra words the candidate lacks ('gupta store station' vs 'Gupta Store').
        extra_query_words = set(query_norm.split()) - set(entry_norm.split())
        set_weight = 0.8 if extra_query_words else 0.95
        base = max(
            fuzz.token_sort_ratio(query_norm, entry_norm),
            fuzz.token_set_ratio(query_norm, entry_norm) * set_weight,
            fuzz.partial_ratio(query_norm, entry_norm) * 0.9 if len(query_norm) >= 4 else 0.0,
        )
        remaining = list(entry_numbers)
        missing = 0
        for number in query_numbers:
            if number in remaining:
                remaining.remove(number)
            else:
                missing += 1
        return max(0.0, base - missing * MISSING_NUMBER_PENALTY - len(remaining) * EXTRA_NUMBER_PENALTY)

    def match(self, query: str, limit: int = 5) -> MatchResult:
        query_norm = normalize(query)
        if not query_norm:
            return MatchResult(query=query)
        query_numbers = numbers(query_norm)
        best: dict[str, Candidate] = {}
        for entry in self._entries:
            s = self.score(query_norm, query_numbers, entry.normalized, entry.numbers)
            if s >= MIN_CANDIDATE_SCORE and (entry.name not in best or s > best[entry.name].score):
                best[entry.name] = Candidate(name=entry.name, score=round(s, 1), matched_text=entry.text)
        ranked = sorted(best.values(), key=lambda c: (-c.score, len(c.name)))
        return MatchResult(query=query, candidates=ranked[:limit])
