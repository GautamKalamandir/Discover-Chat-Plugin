"""Value evidence (ADR 0014): what the user's filter words are in their own data.

The server gathers evidence and never decides what the user meant:
- every string filter value (=, in, <>) and every word the plan couldn't place is looked up AS
  THE USER (Fabric IQ ValueSearch, so RLS applies): first scoped to the column the plan used, then
  model-wide;
- when ValueSearch is unavailable, the column's distinct values are read with a bounded DAX query
  (as the user, cached briefly per user) and ranked locally;
- a value stored exactly (ignoring case) is switched to its stored spelling; anything else goes back
  to the LLM, which decides: use a stored value, ask the user with options, or say it can't answer.

Values are user data: they are never logged (Q18), only counted.
"""

import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from app.agent.dax_builder import column as dax_column
from app.agent.models import Filter, QueryPlan, parse_ref
from app.agent.values import same_column
from app.authz.models import AuthorizedContext
from app.core.config import Settings
from app.core.errors import AppError
from app.powerbi.service import PowerBIService

logger = logging.getLogger(__name__)

MAX_TERMS_PER_SEARCH = 20  # ValueSearch accepts 1-20 search terms
CANDIDATES_PER_TERM = 5
MIN_SIMILARITY = 0.5  # local ranking noise floor (fallback only); ValueSearch scores are kept as is
MAX_CACHED_COLUMNS = 2000
_VALUE_OPS = ("=", "in", "<>")
_NON_WORD = re.compile(r"[^0-9a-z]+")


@dataclass(frozen=True)
class Candidate:
    column: str  # 'Table'[Column]
    value: str
    score: float

    def describe(self) -> str:
        return f'{self.column} = "{self.value}" (match score {self.score:.2f})'


@dataclass
class TermEvidence:
    model_id: str
    term: str
    column: str | None  # where the plan used the word; None = a word the plan couldn't place
    searched: bool = False  # False: no lookup was possible (no evidence either way)
    exact: str | None = None  # stored spelling when the value exists in `column`
    candidates: list[Candidate] = field(default_factory=list)

    def key(self) -> tuple[str, str, str]:
        return self.model_id, self.term.lower(), (self.column or "").lower()


@dataclass
class Evidence:
    terms: list[TermEvidence] = field(default_factory=list)

    def merged(self, other: "Evidence") -> "Evidence":
        seen = {t.key() for t in self.terms}
        return Evidence(self.terms + [t for t in other.terms if t.key() not in seen])

    def covers(self, model_id: str, term: str, column: str | None) -> bool:
        wanted = (model_id, term.lower(), (column or "").lower())
        return any(t.key() == wanted for t in self.terms)

    def needs_decision(self) -> bool:
        """The LLM must look at the evidence: a filter value isn't stored as written, or a word it
        couldn't place was found in the data."""
        return any(
            (t.column is not None and t.searched and t.exact is None)
            or (t.column is None and t.candidates)
            for t in self.terms
        )

    def stored_value(self, model_id: str, column: str, value: str) -> str | None:
        """The stored spelling of `value` in `column`, if the evidence shows it exists there."""
        wanted = value.strip().lower()
        for term in self.terms:
            if term.model_id != model_id:
                continue
            if term.exact and term.exact.lower() == wanted and _same(term.column, column):
                return term.exact
            for candidate in term.candidates:
                if candidate.value.lower() == wanted and same_column(candidate.column, column):
                    return candidate.value
        return None

    def checkable(self, model_id: str) -> bool:
        """Whether this model's values could be looked up at all (else nothing can be checked)."""
        return any(t.searched for t in self.terms if t.model_id == model_id)

    def lines(self) -> list[str]:
        """What the LLM is shown: only words that aren't stored exactly as written."""
        out: list[str] = []
        for t in self.terms:
            if t.column is not None and t.exact is not None:
                continue
            closest = "; ".join(c.describe() for c in t.candidates)
            if t.column is None:
                if t.candidates:
                    out.append(f'"{t.term}": {closest}')
                continue
            if not t.searched:
                out.append(f'"{t.term}" (used for {t.column}): could not be looked up')
            elif closest:
                out.append(f'"{t.term}" (used for {t.column}): not stored as written. {closest}')
            else:
                out.append(f'"{t.term}" (used for {t.column}): nothing similar is stored')
        return out


def _same(a: str | None, b: str) -> bool:
    return a is not None and same_column(a, b)


# --- gathering -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Wanted:
    term: str
    column: str | None


def _value_filters(plan: QueryPlan) -> Iterable[tuple[str, Filter]]:
    for step in plan.steps:
        for flt in step.filters:
            yield step.model_id, flt
    if plan.change is not None:
        for flt in plan.change.filters:
            yield plan.change.model_id, flt


def _wanted(
    plan: QueryPlan, unresolved_models: Iterable[str], known: Evidence
) -> dict[str, list[_Wanted]]:
    wanted: dict[str, list[_Wanted]] = {}

    def add(model_id: str, term: str, column: str | None) -> None:
        term = term.strip()[:100]
        if not term or known.covers(model_id, term, column):
            return
        items = wanted.setdefault(model_id, [])
        if _Wanted(term, column) not in items:
            items.append(_Wanted(term, column))

    for model_id, flt in _value_filters(plan):
        if flt.op in _VALUE_OPS:
            for value in flt.values:
                if isinstance(value, str):
                    add(model_id, value, flt.column)
    for model_id in unresolved_models:
        for term in plan.unresolved_terms[:5]:
            add(model_id, term, None)
    return wanted


class ValueEvidence:
    """Gathers evidence for a plan's value words, as the user."""

    def __init__(
        self,
        powerbi: PowerBIService,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._powerbi = powerbi
        self._fallback_rows = min(
            settings.value_fallback_max_values, settings.powerbi_max_rows_limit
        )
        self._ttl = settings.value_cache_minutes * 60
        self._clock = clock
        self._cache: dict[tuple[str, str, str], tuple[float, list[str]]] = {}

    async def gather(
        self,
        plan: QueryPlan,
        authz: AuthorizedContext,
        *,
        unresolved_models: Iterable[str] = (),
        known: Evidence | None = None,
    ) -> Evidence:
        known = known or Evidence()
        found: list[TermEvidence] = []
        for model_id, items in _wanted(plan, unresolved_models, known).items():
            found += await self._for_model(authz, model_id, items)
        if found:
            logger.info(
                "Value evidence: %d words, %d stored as written, %d with candidates",
                len(found),
                sum(1 for t in found if t.exact),
                sum(1 for t in found if t.candidates),
            )
        return known.merged(Evidence(found))

    async def _for_model(
        self, authz: AuthorizedContext, model_id: str, items: list[_Wanted]
    ) -> list[TermEvidence]:
        evidence = [TermEvidence(model_id, w.term, w.column) for w in items]
        bound = [e for e in evidence if e.column is not None]

        # 1. Scoped to the columns the plan used.
        scoped = await self._search(authz, model_id, [e.term for e in bound], _scope(bound))
        for e in bound:
            if scoped is None:
                await self._fallback(authz, e)
                continue
            e.searched = True
            e.candidates = _best(scoped.get(e.term.lower(), []))
            e.exact = next(
                (
                    c.value
                    for c in e.candidates
                    if same_column(c.column, e.column or "") and c.value.lower() == e.term.lower()
                ),
                None,
            )

        # 2. Model-wide for words not found in their column, and for words the plan couldn't place.
        #    Skipped when ValueSearch just proved unavailable.
        rest = (
            []
            if scoped is None
            else [
                e
                for e in evidence
                if e.column is None or not any(_same(c.column, e.column) for c in e.candidates)
            ]
        )
        wide = await self._search(authz, model_id, [e.term for e in rest], None)
        for e in rest:
            if wide is None:
                continue
            e.searched = True
            e.candidates = _best(e.candidates + wide.get(e.term.lower(), []))
        return evidence

    async def _search(
        self,
        authz: AuthorizedContext,
        model_id: str,
        terms: list[str],
        scope: list[dict[str, Any]] | None,
    ) -> dict[str, list[Candidate]] | None:
        """Candidates per lower-cased term, or None when ValueSearch is unavailable."""
        unique = list(dict.fromkeys(terms))[:MAX_TERMS_PER_SEARCH]
        if not unique:
            return {}
        try:
            payload = await self._powerbi.search_values(
                authz, model_id=model_id, terms=unique, scope=scope
            )
        except AppError as exc:
            logger.info("Value search unavailable for %s: %s", model_id, exc.code)
            return None
        return _candidates_by_term(payload.data)

    async def _fallback(self, authz: AuthorizedContext, evidence: TermEvidence) -> None:
        """ValueSearch unavailable: rank the column's distinct values locally (D5)."""
        column = evidence.column or ""
        values = await self._column_values(authz, evidence.model_id, column)
        if values is None:
            return
        evidence.searched = True
        quoted = _quoted(column)
        ranked = [Candidate(quoted, v, similarity(evidence.term, v)) for v in values]
        evidence.candidates = _best([c for c in ranked if c.score >= MIN_SIMILARITY])
        evidence.exact = next((v for v in values if v.lower() == evidence.term.lower()), None)

    async def _column_values(
        self, authz: AuthorizedContext, model_id: str, column: str
    ) -> list[str] | None:
        key = (authz.request.user.key, model_id, column.lower())  # per user: RLS
        now = self._clock()
        cached = self._cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
        if parse_ref(column) is None:
            return None
        ref = dax_column(column)
        dax = f"EVALUATE TOPN({self._fallback_rows}, VALUES({ref}), {ref}, ASC)"
        try:
            result = await self._powerbi.execute_query(
                authz, model_id=model_id, dax=dax, max_rows=self._fallback_rows
            )
        except AppError as exc:
            logger.info("Column values unavailable for %s: %s", model_id, exc.code)
            return None
        returned = next((c for c in result.columns if same_column(c, column)), None)
        if returned is None:  # not the column we asked for: no evidence rather than wrong evidence
            logger.info("Column values for %s came back in an unexpected shape", model_id)
            return None
        values = [str(row[returned]) for row in result.rows if row.get(returned) not in (None, "")]
        if len(self._cache) >= MAX_CACHED_COLUMNS:
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            if len(self._cache) >= MAX_CACHED_COLUMNS:
                self._cache.clear()
        self._cache[key] = (now + self._ttl, values)
        return values


def _scope(items: list[TermEvidence]) -> list[dict[str, Any]] | None:
    tables: dict[str, list[str]] = {}
    for item in items:
        parsed = parse_ref(item.column or "")
        if parsed is not None and parsed[1] not in tables.setdefault(parsed[0], []):
            tables[parsed[0]].append(parsed[1])
    return [{"tableName": t, "columnNames": cols} for t, cols in tables.items()] or None


def _candidates_by_term(payload: Any) -> dict[str, list[Candidate]]:
    """Spike S3 shape: {"Results": {"<term>": [{"Table", "Column", "Value", "Score"}]}}."""
    results = payload.get("Results") if isinstance(payload, dict) else None
    if not isinstance(results, dict):
        return {}
    found: dict[str, list[Candidate]] = {}
    for term, matches in results.items():
        if not isinstance(matches, list):
            continue
        for match in matches:
            if not isinstance(match, dict):
                continue
            table, column, value = match.get("Table"), match.get("Column"), match.get("Value")
            score = match.get("Score")
            if isinstance(table, str) and isinstance(column, str) and value is not None:
                found.setdefault(str(term).lower(), []).append(
                    Candidate(
                        f"'{table}'[{column}]",
                        str(value),
                        float(score) if isinstance(score, int | float) else 0.0,
                    )
                )
    return found


def _best(candidates: list[Candidate]) -> list[Candidate]:
    unique: dict[tuple[str, str], Candidate] = {}
    for c in candidates:
        key = (c.column.lower(), c.value.lower())
        if key not in unique or c.score > unique[key].score:
            unique[key] = c
    return sorted(unique.values(), key=lambda c: -c.score)[:CANDIDATES_PER_TERM]


def _quoted(column: str) -> str:
    parsed = parse_ref(column)
    return f"'{parsed[0]}'[{parsed[1]}]" if parsed else column


def _norm(text: str) -> str:
    return " ".join(_NON_WORD.sub(" ", text.lower()).split())


def similarity(term: str, value: str) -> float:
    """0..1, for ranking a column's values against a word (fallback only). A word that is a whole
    word of the value ("kosamba" in "STR - KOSAMBA") ranks high; typos rank by closeness."""
    a, b = _norm(term), _norm(value)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    words = b.split()
    score = SequenceMatcher(None, a, b).ratio()
    if a in words or a.replace(" ", "") == b.replace(" ", ""):
        score = max(score, 0.95)
    elif a in b:
        score = max(score, 0.85)
    best_word = max((SequenceMatcher(None, a, w).ratio() for w in words), default=0.0)
    return round(max(score, best_word * 0.95), 2)


# --- applying -----------------------------------------------------------------------------------


def apply_stored_values(plan: QueryPlan, evidence: Evidence) -> QueryPlan:
    """Switches every filter value the evidence shows as stored to its stored spelling."""

    def fix(model_id: str, filters: list[Filter]) -> list[Filter]:
        fixed = []
        for flt in filters:
            values = list(flt.values)
            if flt.op in _VALUE_OPS:
                for i, value in enumerate(values):
                    if isinstance(value, str):
                        values[i] = evidence.stored_value(model_id, flt.column, value) or value
            fixed.append(flt.model_copy(update={"values": values}))
        return fixed

    steps = [s.model_copy(update={"filters": fix(s.model_id, s.filters)}) for s in plan.steps]
    change = plan.change
    if change is not None:
        change = change.model_copy(update={"filters": fix(change.model_id, change.filters)})
    return plan.model_copy(update={"steps": steps, "change": change})


def unknown_values(plan: QueryPlan, evidence: Evidence) -> list[str]:
    """Filter values (=, in, <>) that the evidence doesn't show as stored, for models whose values
    could be checked. Used to make sure an option the user is offered only uses real values."""
    problems: list[str] = []
    for model_id, flt in _value_filters(plan):
        if flt.op not in _VALUE_OPS or not evidence.checkable(model_id):
            continue
        for value in flt.values:
            if (
                isinstance(value, str)
                and evidence.stored_value(model_id, flt.column, value) is None
            ):
                problems.append(f"{flt.column} has no stored value {value!r}")
    return problems


def step_models(plan: QueryPlan) -> list[str]:
    models: list[str] = [s.model_id for s in plan.steps]
    if plan.change is not None:
        models.append(plan.change.model_id)
    return list(dict.fromkeys(models))
