"""Numbers and text as a /summary report shows them (ADR 0015 amendment 1).

- Amounts use the Indian system: 1,47,61,530.70 in tables; 1.48 Cr / 14.76 lakh when compact.
  Only the server formats numbers, so the text, tables and charts always agree.
- LLM text is cleaned before it is shown: characters the PDF font lacks are replaced with plain
  equivalents, and evidence ids (k2.prev_day, f1) are removed.
"""

import math
import re

# Characters the LLM likes that Helvetica/WinAnsi can't draw (shown as a black box in the first
# live PDF), by code point so the source holds no invisible characters.
_REPLACEMENTS = {
    0x2010: "-",  # hyphen
    0x2011: "-",  # non-breaking hyphen
    0x2012: "-",  # figure dash
    0x2013: "-",  # en dash
    0x2014: " - ",  # em dash
    0x2015: " - ",  # horizontal bar
    0x2212: "-",  # minus sign
    0x00A0: " ",  # non-breaking space
    0x202F: " ",  # narrow non-breaking space
    0x2009: " ",  # thin space
    0x2018: "'",  # curly quotes
    0x2019: "'",
    0x201C: '"',
    0x201D: '"',
    0x2026: "...",  # ellipsis
    0x20B9: "Rs ",  # rupee sign (not in the PDF font)
}
_TRANSLATE = str.maketrans(_REPLACEMENTS)
# Evidence ids the agent may copy into its text: (k2.prev_day), k1.mtd, (f1), [f2].
_ID = r"[kf]\d+(?:\.[a-z_]+)?"
_FACT_ID = re.compile(rf"\s*[(\[]\s*{_ID}(?:\s*[,;/]\s*{_ID})*\s*[)\]]")
_BARE_FACT_ID = re.compile(r"\b[k]\d+\.(?:prev_day|week_ago|mtd|target_day|target_mtd)\b")
_SPACES = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([.,;:!?])")


def format_indian(value: float | None, decimals: int | None = None) -> str:
    """1,47,61,530.70 (two decimals unless the number is whole)."""
    if value is None or not math.isfinite(value):
        return "n/a"
    places = decimals if decimals is not None else (0 if float(value).is_integer() else 2)
    text = f"{abs(value):.{places}f}"
    whole, _, fraction = text.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join([*groups, tail])
    sign = "-" if value < 0 and float(text) != 0 else ""
    return sign + whole + (f".{fraction}" if fraction else "")


def compact_indian(value: float | None) -> str:
    """1.48 Cr, 14.76 lakh, 5,320 or 12.5 (charts and headlines)."""
    if value is None or not math.isfinite(value):
        return "n/a"
    size = abs(value)
    if size >= 1e7:
        return f"{value / 1e7:.2f} Cr"
    if size >= 1e5:
        return f"{value / 1e5:.2f} lakh"
    return format_indian(round(value, 2))


def percent(value: float | None) -> str:
    return "n/a" if value is None else f"{abs(value):.2f}%"


def change_words(change: float | None, pct: float | None) -> str:
    """'down 1,94,14,359.36 (56.80%)', 'up 5 (2.00%)' or 'no change': never a double negative."""
    if change is None:
        return "n/a"
    if change == 0:
        return "no change"
    direction = "up" if change > 0 else "down"
    amount = format_indian(abs(change))
    return f"{direction} {amount}" + (f" ({percent(pct)})" if pct is not None else "")


def clean_text(text: str) -> str:
    """LLM text ready to show: plain characters, no evidence ids, tidy spacing."""
    text = text.translate(_TRANSLATE)
    text = _FACT_ID.sub("", text)
    text = _BARE_FACT_ID.sub("", text)
    text = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text)
    lines = [_SPACES.sub(" ", line).rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip()
