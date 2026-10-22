"""The four tables of a /sales-summary, built by the server from the loop's facts (design §6).

The main table of each lens comes from the server's own first query (fixed shape). Every further
query the agent ran becomes a drill-down table under it. Every cell carries the id of the fact it
shows, and its colour comes from that fact (green: better for that measure, red: worse); there are
no thresholds. Figures the server computes (stock turn, share of sales) become facts too, so text
can cite them.
"""

from collections.abc import Callable
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from app.agent.models import parse_ref
from app.reports.models import Suggestion
from app.reports.sales.discovery import ModelRoles
from app.reports.sales.lenses import KEYS, TITLES, Lens
from app.reports.sales.loop import LoopResult
from app.reports.sales.periods import SalesPeriods
from app.reports.sales.results import Fact, StepResult
from app.reports.text import format_indian

Tone = Literal["good", "bad", "neutral"]
MAX_MAIN_ROWS = 20
DRILL_ROWS = 8  # largest falls and rises each way in a drill-down table
PERIOD_NAMES = {"day": "Day", "mtd": "MTD", "fytd": "FYTD"}


class Cell(BaseModel):
    text: str
    tone: Tone | None = None
    fact: str | None = None  # the fact it shows


class Table(BaseModel):
    title: str
    columns: list[str]
    rows: list[list[Cell]] = Field(default_factory=list)
    note: str | None = None


class LensTable(BaseModel):
    lens: Lens
    title: str
    status: Literal["ok", "failed", "no_data"] = "ok"
    message: str | None = None
    main: Table | None = None
    drilldowns: list[Table] = Field(default_factory=list)
    findings: str = ""
    suggestions: list[Suggestion] = Field(default_factory=list)
    cut_short: bool = False
    queries_used: int = 0
    notes: list[str] = Field(default_factory=list)
    # Every figure behind the table (the agent's queries and the server's own), for the summary
    # bullets to cite and to be checked against. Not stored in report.json.
    facts: list[Fact] = Field(default_factory=list, exclude=True)


Key = tuple[int, str, str, str | None, str | None]  # row, measure id, period, month, band


def build_table(
    lens: Lens, result: LoopResult, roles: ModelRoles, periods: SalesPeriods
) -> tuple[LensTable, list[Fact]]:
    """The table and the facts the server computed for it (to cite and to ground text)."""
    seed = result.steps[0]
    derived: list[Fact] = []
    main_step: StepResult | None = seed
    main: Table | None
    if lens == "lob_growth":
        main = _lob(seed, roles)
    elif lens == "subcategory_loss":
        main = _subcategory(seed, periods)
    elif lens == "stock_turn":
        main, derived = _stock(seed, roles, periods)
    else:
        banded = next((s for s in result.steps if s.step.bands and s.facts), None)
        main_step = seed if seed.step.group_by else banded
        main, derived = _price(main_step) if main_step is not None else (None, [])
    # Every other query the agent ran is a drill-down (the one shown as the main table isn't).
    drills = [_drill(s) for s in result.steps[1:] if s.facts and s is not main_step]
    notes = [
        f"Not found in the data of {s.model_id}: {', '.join(s.unmatched)}."
        for s in result.steps
        if s.unmatched
    ]
    if lens == "price_range" and main is None:
        notes.append("No price ranges were set for this data source.")
    table = LensTable(
        lens=lens,
        title=TITLES[lens],
        main=main,
        drilldowns=[d for d in drills if d.rows],
        cut_short=result.cut_short,
        queries_used=result.queries_used,
        notes=notes,
    )
    return table, derived


# --- the main tables ----------------------------------------------------------------------------


def _lob(seed: StepResult, roles: ModelRoles) -> Table:
    index, rows = _index(seed.facts)
    group = seed.step.group_by[0]
    others = [(f"m{i}", ref) for i, ref in enumerate(seed.step.measures, start=1)][1:]
    columns = [_name(group), "Day vs LY", "MTD", "MTD vs LY", "FYTD", "FYTD vs LY"]
    columns += [f"{_name(ref)} MTD vs LY" for _, ref in others] + ["Status"]
    table_rows = []
    for row, labels in rows:
        mtd = index.get((row, "m1", "mtd", None, None))
        cells = [
            Cell(text=labels.get(group, "")),
            _pct(index.get((row, "m1", "day", None, None))),
            _value(mtd),
            _pct(mtd),
            _value(index.get((row, "m1", "fytd", None, None))),
            _pct(index.get((row, "m1", "fytd", None, None))),
        ]
        cells += [_pct(index.get((row, mid, "mtd", None, None))) for mid, _ in others]
        cells.append(_status(mtd))
        table_rows.append((mtd.value if mtd and mtd.value is not None else 0.0, cells))
    table_rows.sort(key=lambda r: r[0], reverse=True)
    return Table(
        title=f"{TITLES['lob_growth']} against last year",
        columns=columns,
        rows=[cells for _, cells in table_rows],
    )


def _subcategory(seed: StepResult, periods: SalesPeriods) -> Table:
    index, rows = _index(seed.facts)
    group = seed.step.group_by[0]
    months = [m for m, _ in periods.ranges("months_fy") if m]
    columns = [_name(group), "MTD", "MTD vs LY", "FYTD vs LY"]
    columns += [f"{_month(m)} vs LY" for m in months]
    table_rows = []
    for row, labels in rows:
        fytd = index.get((row, "m1", "fytd", None, None))
        cells = [
            Cell(text=labels.get(group, "")),
            _value(index.get((row, "m1", "mtd", None, None))),
            _pct(index.get((row, "m1", "mtd", None, None))),
            _pct(fytd),
        ]
        cells += [_pct(index.get((row, "m1", "months_fy", m, None))) for m in months]
        table_rows.append((fytd.change if fytd and fytd.change is not None else 0.0, cells))
    table_rows.sort(key=lambda r: r[0])  # largest loss first
    note = None
    if len(table_rows) > MAX_MAIN_ROWS:
        note = f"The {MAX_MAIN_ROWS} largest losses of {len(table_rows)} sub-categories."
    return Table(
        title=f"{TITLES['subcategory_loss']} against last year",
        columns=columns,
        rows=[cells for _, cells in table_rows[:MAX_MAIN_ROWS]],
        note=note,
    )


def _stock(seed: StepResult, roles: ModelRoles, periods: SalesPeriods) -> tuple[Table, list[Fact]]:
    index, rows = _index(seed.facts)
    group = seed.step.group_by[0] if seed.step.group_by else None
    first = _name(group) if group else "Data source"
    if roles.measures.get("stock_turn") in seed.step.measures:
        table_rows = []
        for row, labels in rows:
            turn = index.get((row, "m1", "fytd", None, None))
            cells = [Cell(text=labels.get(group, "") if group else ""), _value(turn), _pct(turn)]
            table_rows.append((turn.value if turn and turn.value is not None else 0.0, cells))
        table_rows.sort(key=lambda r: r[0])  # slowest first
        return (
            Table(
                title=f"{TITLES['stock_turn']} this financial year",
                columns=[first, "Stock turn FYTD", "vs LY"],
                rows=[c for _, c in table_rows[:MAX_MAIN_ROWS]],
            ),
            [],
        )

    # Derived (ADR 0016 §8): sales FYTD / average month-end stock, annualised.
    days = (periods.day - periods.fy_start).days + 1
    months = [m for m, _ in periods.ranges("month_ends_fy") if m]
    key = KEYS["stock_turn"]
    derived: list[Fact] = []
    table_rows = []
    for row, labels in rows:
        sales = index.get((row, "m1", "fytd", None, None))
        stocks = [index.get((row, "m2", "month_ends_fy", m, None)) for m in months]
        values = [s.value for s in stocks if s is not None and s.value is not None]
        latest = stocks[-1] if stocks else None
        average = round(sum(values) / len(values), 2) if values else None
        sold = sales.value if sales and sales.value is not None else 0.0
        rate = round(sold / average * 365 / days, 2) if average else None
        computed = [
            _derived(f"{key}.1.r{row}.avg_stock", labels, "Average month-end stock", average),
            _derived(f"{key}.1.r{row}.turn", labels, "Stock turn (annualised)", rate),
        ]
        derived += [f for f in computed if f.value is not None]
        held = latest is not None and (latest.value or 0) > 0
        flag = Cell(text="No sales this year", tone="bad") if held and not sold else Cell(text="")
        cells = [
            Cell(text=labels.get(group, "") if group else ""),
            _value(sales),
            Cell(text=format_indian(average), fact=computed[0].id if average else None),
            Cell(text=format_indian(rate), fact=computed[1].id if rate is not None else None),
            _value(latest),
            flag,
        ]
        table_rows.append((rate if rate is not None else float("inf"), cells))
    table_rows.sort(key=lambda r: r[0])  # slowest first
    return (
        Table(
            title=f"{TITLES['stock_turn']} this financial year (computed from sales and stock)",
            columns=[
                first,
                "Sales FYTD",
                "Average month-end stock",
                "Stock turn (annualised)",
                f"Stock on {periods.day:%d %b}",
                "",
            ],
            rows=[c for _, c in table_rows[:MAX_MAIN_ROWS]],
            note="Stock turn = sales this financial year / average month-end stock, annualised.",
        ),
        derived,
    )


def _price(step: StepResult) -> tuple[Table, list[Fact]]:
    index, rows = _index(step.facts)
    groups = step.step.group_by
    bands: list[str | None] = [b.label for b in step.step.bands] or [None]
    entries: list[tuple[dict[str, str], int, str | None]] = [
        (labels, row, band) for row, labels in rows for band in bands
    ]
    total = sum(
        f.value
        for f in step.facts
        if f.measure_id == "m1" and f.period == "fytd" and f.value is not None
    )
    key = KEYS["price_range"]
    derived: list[Fact] = []
    table_rows = []
    for labels, row, band in entries:
        mtd = index.get((row, "m1", "mtd", None, band))
        fytd = index.get((row, "m1", "fytd", None, band))
        if mtd is None and fytd is None:
            continue
        share = round(fytd.value / total * 100, 2) if fytd and fytd.value and total else None
        cells = [Cell(text=labels.get(g, "")) for g in groups]
        if band is not None:
            cells.append(Cell(text=band))
        share_fact = _derived(
            f"{key}.{step.number}.r{row}.share{f'|{band}' if band else ''}",
            {**labels, **({"Price range": band} if band else {})},
            "Share of sales FYTD (%)",
            share,
        )
        if share is not None:
            derived.append(share_fact)
        cells += [
            _value(mtd),
            _pct(mtd),
            _value(fytd),
            _pct(fytd),
            Cell(text=f"{share:.2f}%" if share is not None else "", fact=share_fact.id),
        ]
        table_rows.append(cells)
    columns = [_name(g) for g in groups] + (["Price range"] if bands != [None] else [])
    columns += ["MTD", "MTD vs LY", "FYTD", "FYTD vs LY", "Share of FYTD"]
    return (
        Table(
            title=f"{TITLES['price_range']} against last year",
            columns=columns,
            rows=table_rows[: MAX_MAIN_ROWS * max(len(bands), 1)],
        ),
        derived,
    )


# --- drill-downs (the agent's own queries) ------------------------------------------------------


def _drill(step: StepResult) -> Table:
    """One of the agent's queries as a table: a value and a change column per measure and period
    (one change column per month), the largest falls and rises first."""
    index, rows = _index(step.facts)
    groups = step.step.group_by
    bands: list[str | None] = [b.label for b in step.step.bands] or [None]
    figures = list(dict.fromkeys((f.measure_id, f.measure, f.period, f.month) for f in step.facts))
    several = len({measure_id for measure_id, *_ in figures}) > 1
    with_ly = {(f.measure_id, f.period, f.month) for f in step.facts if f.ly_value is not None}
    specs: list[tuple[str, str, str, str | None, Callable[[Fact | None], Cell]]] = []
    for measure_id, measure, period, month in figures:
        when = _month(month) if month else PERIOD_NAMES.get(period, period)
        name = f"{measure} {when}" if several else when
        has_ly = (measure_id, period, month) in with_ly
        if month:  # one column per month: the change when there is one, else the value
            specs.append(
                (
                    f"{name} vs LY" if has_ly else name,
                    measure_id,
                    period,
                    month,
                    _pct if has_ly else _value,
                )
            )
            continue
        specs.append((name, measure_id, period, month, _value))
        if has_ly:
            specs.append((f"{name} vs LY", measure_id, period, month, _pct))

    columns = [_name(g) for g in groups] + (["Price range"] if bands != [None] else [])
    columns += [header for header, *_ in specs]
    built: list[tuple[float, list[Cell]]] = []
    for row, labels in rows:
        for band in bands:
            facts = [
                index.get((row, mid, period, month, band)) for _, mid, period, month, _ in specs
            ]
            if not any(facts):
                continue
            cells = [Cell(text=labels.get(g, "")) for g in groups]
            if band is not None:
                cells.append(Cell(text=band))
            cells += [make(fact) for (*_, make), fact in zip(specs, facts, strict=True)]
            built.append((_order(next((f for f in facts if f), None)), cells))
    built.sort(key=lambda r: r[0])
    note = None
    if len(built) > 2 * DRILL_ROWS:
        note = f"The {DRILL_ROWS} largest falls and rises of {len(built)} rows."
        built = built[:DRILL_ROWS] + built[-DRILL_ROWS:]
    return Table(
        title=step.why or f"Query {step.number}",
        columns=columns,
        rows=[c for _, c in built],
        note=note,
    )


def _order(fact: Fact | None) -> float:
    if fact is None:
        return 0.0
    if fact.percent_change is not None:
        return fact.percent_change
    return fact.change if fact.change is not None else -(fact.value or 0.0)


# --- cells and helpers --------------------------------------------------------------------------


def _index(facts: list[Fact]) -> tuple[dict[Key, Fact], list[tuple[int, dict[str, str]]]]:
    index: dict[Key, Fact] = {}
    rows: dict[int, dict[str, str]] = {}
    for fact in facts:
        index[(fact.row_number, fact.measure_id, fact.period, fact.month, fact.band)] = fact
        rows.setdefault(fact.row_number, fact.row)
    return index, list(rows.items())


def _value(fact: Fact | None) -> Cell:
    if fact is None or fact.value is None:
        return Cell(text="")
    return Cell(text=format_indian(fact.value), fact=fact.id)


def _pct(fact: Fact | None) -> Cell:
    """The change against last year: "+12.30%", "-5.00%", or "new" when last year had nothing."""
    if fact is None or fact.change is None:
        return Cell(text="")
    tone = _tone(fact.good)
    if fact.percent_change is None:
        return Cell(text="new" if fact.change > 0 else "", tone=tone, fact=fact.id)
    sign = "+" if fact.percent_change > 0 else ""
    return Cell(text=f"{sign}{fact.percent_change:.2f}%", tone=tone, fact=fact.id)


def _status(fact: Fact | None) -> Cell:
    if fact is None or not fact.change:
        return Cell(text="")
    grew = fact.change > 0
    return Cell(text="Growth" if grew else "Degrowth", tone=_tone(fact.good), fact=fact.id)


def _tone(good: bool | None) -> Tone:
    return "neutral" if good is None else "good" if good else "bad"


def _derived(fact_id: str, row: dict[str, str], measure: str, value: float | None) -> Fact:
    return Fact(id=fact_id, row=row, measure=measure, period="fytd", value=value)


def _name(ref: str) -> str:
    parsed = parse_ref(ref)
    return parsed[1] if parsed else ref


def _month(key: str) -> str:
    return f"{date.fromisoformat(f'{key}-01'):%b %Y}"
