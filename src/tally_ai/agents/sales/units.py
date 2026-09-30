"""Unit words as people type them, mapped to a canonical key."""

UNIT_SYNONYMS = {
    "ctn": {"ctn", "ctns", "carton", "cartons", "cartoon", "cartoons", "crtn", "peti", "cs", "case", "cases"},
    "pcs": {"pc", "pcs", "piece", "pieces", "nos", "no", "nag"},
    "box": {"box", "boxes", "bx"},
    "pkt": {"pkt", "pkts", "packet", "packets", "pack", "packs"},
    "kg": {"kg", "kgs", "kilo", "kilos"},
}

ALL_UNIT_WORDS = sorted({w for names in UNIT_SYNONYMS.values() for w in names} | set(UNIT_SYNONYMS), key=len)


def unit_key(unit: str) -> str:
    u = unit.strip().lower().rstrip(".")
    for key, names in UNIT_SYNONYMS.items():
        if u == key or u in names:
            return key
    return u
