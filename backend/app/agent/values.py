"""Column references from different sources, compared as the same column.

The planner writes `Table[Column]`; Fabric IQ ValueSearch results are qualified as `'Table'[Column]`
(spike S3). Value lookup itself lives in `app/agent/resolution.py` (ADR 0014).
"""

from app.agent.models import parse_ref


def same_column(a: str, b: str) -> bool:
    pa, pb = parse_ref(a), parse_ref(b)
    if pa is None or pb is None:
        return False
    return (pa[0].lower(), pa[1].lower()) == (pb[0].lower(), pb[1].lower())
