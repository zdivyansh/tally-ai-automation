"""Recover customer and item text from the message itself.

Small models drop words ('kk fl & puff 120 10/-' -> 'kk fl & puff 10/-',
'gupta store main road' -> 'main road'). These helpers find what the user
actually typed in the usual positions so the extraction can be *extended*
with the user's own words, never changed or invented.
"""

import re

from tally_ai.agents.sales.units import ALL_UNIT_WORDS

_UNIT = "|".join(re.escape(u) for u in reversed(ALL_UNIT_WORDS))
_ANCHOR = re.compile(rf"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:{_UNIT})\b\.?\s+", re.I)
# Where an item's text ends
_ITEM_END = re.compile(
    r"\s*(?:,|\+|\baur\b|\band\b|\bto\b|\bko\b|\bke liye\b|\bdiya\b|\bdiye\b|\bbheja\b|\bbheje\b|\bbecha\b"
    r"|\bsold\b|@|\brate\b|\bless\b|\bdisc(?:ount)?\b|\d+(?:\.\d+)?\s*%)",
    re.I,
)
_DATE_WORDS = re.compile(
    r"^(?:(?:aaj|kal|parso|parson|today|yesterday)\s+)+|(?:\s+(?:aaj|kal|parso|parson|today|yesterday))+$",
    re.I,
)
_LEADING_VERBS = re.compile(r"^(?:bill|sold|sale|becha|bheja|diya)\s+", re.I)
_PACK_TOKEN = re.compile(r"^\(?\d+(?:\.\d+)?\s*[a-z]{0,3}\)?$", re.I)  # 288p, (120), 150ml


def _canon(token: str) -> str:
    return re.sub(r"[`'’]", "'", token.lower()).strip(",.")


def item_chunks(message: str) -> list[tuple[float, str]]:
    """(quantity, text) for each '<qty> <unit> <item text>' in the message."""
    anchors = list(_ANCHOR.finditer(message))
    chunks = []
    for i, m in enumerate(anchors):
        end = anchors[i + 1].start() if i + 1 < len(anchors) else len(message)
        text = message[m.end() : end]
        stop = _ITEM_END.search(text)
        if stop:
            text = text[: stop.start()]
        text = " ".join(text.split())
        if text:
            chunks.append((float(m.group(1)), text))
    return chunks


def extend_to_typed(extracted: str, typed: str, *, trailing_packs: bool = True) -> str:
    """The typed text from its start up to the extracted words (plus pack sizes right after them).

    Returns `extracted` unchanged unless all its words appear in `typed`, in order.
    """
    typed_tokens = typed.split()
    canon = [_canon(t) for t in typed_tokens]
    position = -1
    for token in extracted.split():
        try:
            position = canon.index(_canon(token), position + 1)
        except ValueError:
            return extracted
    if position < 0:
        return extracted
    end = position + 1
    while trailing_packs and end < len(typed_tokens) and _PACK_TOKEN.match(typed_tokens[end]):
        end += 1
    return " ".join(typed_tokens[:end])


def party_span(message: str) -> str | None:
    """Customer text in the usual positions: '<party> ko ...', '... to <party>', 'bill <party> <qty>'."""
    text = " ".join(message.split())
    candidates = []
    if m := re.match(r"^(.*?)\s+(?:ko|ke liye)\s+", text, re.I):
        candidates.append(m.group(1))
    if m := re.search(r"\bto\s+(.+)$", text, re.I):
        candidates.append(m.group(1))
    if m := re.match(r"^bill\s+(.+?)\s+\d", text, re.I):
        candidates.append(m.group(1))
    for candidate in candidates:
        if _ANCHOR.search(candidate + " "):  # contains an item: not a clean customer span
            continue
        span = _DATE_WORDS.sub("", _LEADING_VERBS.sub("", candidate.strip())).strip(" ,.")
        if span:
            return span
    return None
