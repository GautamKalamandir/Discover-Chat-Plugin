"""Prompts of the /sales-summary agent (ADR 0016): every one starts from the shared sales persona (a
senior MIS executive at a jewellery brand, with the research principles of ADR 0016)."""

import functools
from pathlib import Path

from app.core.config import Settings
from app.reports.sales.lenses import LENSES, Lens

PROMPTS = Path(__file__).parent.parent / "prompts"


@functools.cache
def _read(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


def load_sales_prompt(name: str) -> str:
    """With the full persona and its principles: for the text a reader sees (findings, bullets)."""
    return _read("sales_persona") + "\n" + _read(name)


def load_compact_prompt(name: str) -> str:
    """With the short persona: for choosing tables, fields and queries, where every request must
    stay small (ADR 0016 amendment 1)."""
    return _read("sales_persona_short") + "\n" + _read(name)


def lens_goals(settings: Settings) -> dict[Lens, str]:
    """What each table must find out, for the investigation loop."""
    return {
        lens: _read(f"sales_lens_{lens}")
        .replace("{root_cause_lobs}", str(settings.sales_report_root_cause_lobs))
        .strip()
        for lens in LENSES
    }
