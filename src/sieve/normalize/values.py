"""Pure value normalizers.

Every function here takes a raw cell value and returns a normalized value or
None. They never raise on bad input; a value they cannot interpret comes back
as None so the caller can record an issue and carry the row forward.

These are the lowest layer in the codebase. Everything above depends on them
and they depend on nothing, which is what makes them cheap to test.
"""

from __future__ import annotations

import math
import re
from typing import Any

__all__ = [
    "clean_str",
    "normalize_domain",
    "normalize_linkedin_url",
    "normalize_company_name",
    "normalize_person_name",
    "normalize_email",
    "normalize_phone",
    "to_int",
    "to_float",
    "to_bool",
    "parse_tenure_months",
    "split_multi",
]

# --------------------------------------------------------------------------
# strings
# --------------------------------------------------------------------------

# Deliberately excludes "na" and "unknown". Both are legitimate values in this
# data: "NA" is North America in the campaign Region column, and "unknown" is a
# valid email-validation status. Pandas treats "NA" as null by default, which
# is how 80 North America rows read as blank during manual profiling.
_NULLISH = {"", "n/a", "#n/a", "none", "null", "nan", "-", "--"}


def clean_str(value: Any) -> str | None:
    """Collapse whitespace and map placeholder text to None."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).replace("\u00a0", " ").strip()
    text = re.sub(r"\s+", " ", text)
    if text.lower() in _NULLISH:
        return None
    return text


def split_multi(value: Any, seps: str = ";,|") -> list[str]:
    """Split a delimited cell into its parts. Returns [] for empty input."""
    text = clean_str(value)
    if text is None:
        return []
    pattern = "[" + re.escape(seps) + "]"
    return [p for p in (clean_str(part) for part in re.split(pattern, text)) if p]


# --------------------------------------------------------------------------
# domains
# --------------------------------------------------------------------------

_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.I)
# Suffixes that need two labels kept, so "bbc.co.uk" survives instead of "co.uk".
_TWO_LABEL_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au",
    "co.nz", "com.sg", "com.my", "com.br", "co.jp", "co.in", "co.za",
    "com.cn", "com.hk", "com.tw", "co.kr", "com.mx", "com.tr", "co.id",
    "com.ph", "co.th", "com.vn",
}


def normalize_domain(value: Any) -> str | None:
    """Reduce a URL or bare domain to a lowercase registrable domain.

    'https://WWW.Adform.com/products/' -> 'adform.com'
    'www.bbc.co.uk'                    -> 'bbc.co.uk'
    """
    text = clean_str(value)
    if text is None:
        return None
    text = _SCHEME.sub("", text).strip()
    text = text.split("/")[0].split("?")[0].split("#")[0]
    text = text.split("@")[-1]          # tolerate an email being passed in
    text = text.split(":")[0]           # drop a port
    text = text.strip(".").lower()
    if text.startswith("www."):
        text = text[4:]
    if not text or "." not in text:
        return None
    if not re.fullmatch(r"[a-z0-9.\-]+", text):
        return None
    labels = text.split(".")
    if len(labels) <= 2:
        return text
    last_two = ".".join(labels[-2:])
    keep = 3 if last_two in _TWO_LABEL_SUFFIXES else 2
    return ".".join(labels[-keep:])


# --------------------------------------------------------------------------
# LinkedIn
# --------------------------------------------------------------------------

_LI_PUBLIC = re.compile(r"/in/([^/?#]+)", re.I)
_LI_SALES = re.compile(r"/sales/(?:lead|people)/([^/?#,]+)", re.I)


def normalize_linkedin_url(value: Any) -> str | None:
    """Reduce any LinkedIn person URL to 'linkedin.com/in/<slug>'.

    Handles the public form, the Sales Navigator lead form, and the
    scheme/www/trailing-slash variation seen across every source file.
    Returns None for a company URL, which belongs on the account instead.
    """
    text = clean_str(value)
    if text is None:
        return None
    if "/company/" in text.lower():
        return None
    match = _LI_PUBLIC.search(text) or _LI_SALES.search(text)
    if match is None:
        return None
    slug = match.group(1).strip().strip("/").lower()
    if not slug:
        return None
    return f"linkedin.com/in/{slug}"


# --------------------------------------------------------------------------
# names
# --------------------------------------------------------------------------

_LEGAL_SUFFIXES = [
    "incorporated", "corporation", "limited", "holdings", "group",
    "inc", "llc", "llp", "ltd", "plc", "corp", "co", "gmbh", "ag", "sa",
    "nv", "bv", "ab", "oy", "as", "aps", "spa", "srl", "pty", "pte",
    "berhad", "bhd", "sdn", "kk", "pt", "sas", "sarl", "kft", "doo",
]
_SUFFIX_RE = re.compile(
    r"[\s,]+(?:" + "|".join(_LEGAL_SUFFIXES) + r")\.?$", re.I
)


def normalize_company_name(value: Any) -> str | None:
    """Casefold a company name and strip legal suffixes and punctuation.

    '33Across Inc.'          -> '33across'
    'Hong Leong Bank Berhad' -> 'hong leong bank'
    Used as a matching key. The original always survives in `name_raw`.
    """
    text = clean_str(value)
    if text is None:
        return None
    text = re.sub(r"\([^)]*\)", " ", text)          # drop parenthetical notes
    text = text.replace("&", " and ")
    previous = None
    while previous != text:                          # 'Foo Group Ltd' needs two passes
        previous = text
        text = _SUFFIX_RE.sub("", text).strip(" ,.")
    text = re.sub(r"[^\w\s\-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text or None


def normalize_person_name(value: Any) -> str | None:
    """Casefold a person name and strip punctuation, keeping word order."""
    text = clean_str(value)
    if text is None:
        return None
    text = re.sub(r"[^\w\s\-']", " ", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text or None


# --------------------------------------------------------------------------
# contact details
# --------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(value: Any) -> str | None:
    """Lowercase an email and confirm it has a plausible shape."""
    text = clean_str(value)
    if text is None:
        return None
    text = text.lower().strip("<>,;")
    if not _EMAIL_RE.match(text):
        return None
    return text


def normalize_phone(value: Any) -> str | None:
    """Reduce a phone number to digits with a leading '+' when present.

    Keeps the number recognisable rather than attempting E.164, since the
    source files mix '+6072265525', '65-533-9898' and '646-606-2174' with no
    reliable country context.
    """
    text = clean_str(value)
    if text is None:
        return None
    has_plus = text.lstrip().startswith("+")
    digits = re.sub(r"\D", "", text)
    if len(digits) < 6:
        return None
    return ("+" if has_plus else "") + digits


# --------------------------------------------------------------------------
# numbers
# --------------------------------------------------------------------------

def to_float(value: Any) -> float | None:
    """Parse a number that may carry currency symbols, commas or a percent."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None if (isinstance(value, float) and math.isnan(value)) else float(value)
    text = clean_str(value)
    if text is None:
        return None
    text = text.replace(",", "").replace("$", "").replace("%", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if match is None:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def to_int(value: Any) -> int | None:
    """Parse an integer, tolerating floats and formatted strings."""
    number = to_float(value)
    if number is None:
        return None
    return int(round(number))


_TRUE = {"true", "yes", "y", "1", "t"}
_FALSE = {"false", "no", "n", "0", "f"}


def to_bool(value: Any) -> bool | None:
    """Parse a boolean from the several spellings the sources use."""
    if isinstance(value, bool):
        return value
    text = clean_str(value)
    if text is None:
        return None
    lowered = text.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    return None


# --------------------------------------------------------------------------
# tenure
# --------------------------------------------------------------------------

_TENURE_RE = re.compile(
    r"(?:(?P<years>\d+)\s*(?:years?|yrs?))?\s*"
    r"(?:(?P<months>\d+)\s*(?:months?|mos?))?",
    re.I,
)


def parse_tenure_months(value: Any) -> int | None:
    """Convert PhantomBuster tenure text into a month count.

    '4 years 2 months in role' -> 50
    '11 months in company'     -> 11
    """
    text = clean_str(value)
    if text is None:
        return None
    match = _TENURE_RE.search(text)
    if match is None:
        return None
    years = int(match.group("years") or 0)
    months = int(match.group("months") or 0)
    total = years * 12 + months
    return total or None
