"""Map a medicine name to its generic using drug_master. An exact lookup, never a guess."""
import re

# Forms say nothing about which medicine it is.
IGNORED_WORDS = {"tablet", "tablets", "tab", "tabs", "capsule", "capsules", "cap", "caps"}
# Numbers with any unit: 500, 500mg, 500 mg, 0.5, 3/850, 60k. Digits inside a word, as in b12, stay.
NUMBER = re.compile(r"(?<![a-z0-9])\d[\d.,/]*(?:\s*(?:mg|mcg|gm|g|ml|iu)\b|[a-z]*)")
UNMATCHED_CONFIDENCE_CAP = 0.7


def name_key(name):
    """'Tab. Glycomet 500 mg' becomes 'glycomet'."""
    words = (word.strip(".,;:()-/") for word in NUMBER.sub(" ", name.lower()).split())
    return " ".join(word for word in words if word and word not in IGNORED_WORDS)


def normalise_name(conn, raw):
    """Return {drug_norm, unmatched, confidence_cap}.

    A brand maps to its generic and a generic maps to itself. A name listed under two
    generics counts as no match, because picking one would be a guess.
    """
    key = name_key(raw)
    rows = conn.execute("SELECT brand, generic FROM drug_master WHERE generic <> ''").fetchall()
    generics = {row["generic"].lower() for row in rows if name_key(row["generic"]) == key}
    if not generics:
        generics = {row["generic"].lower() for row in rows if name_key(row["brand"] or "") == key}
    if key and len(generics) == 1:
        return {"drug_norm": generics.pop(), "unmatched": False, "confidence_cap": 1.0}
    return {"drug_norm": raw.strip().lower(), "unmatched": True, "confidence_cap": UNMATCHED_CONFIDENCE_CAP}
