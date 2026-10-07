"""Every security scenario (IMPLEMENTATION_PLAN.md §7) keeps at least one automated test
(ADR 0011).

Backend tests carry `@pytest.mark.scenario(N)`; the visual's tests carry an `// @scenario N`
comment.
Removing the last test for a scenario fails this test.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCENARIOS = set(range(1, 23))


def covered() -> dict[int, list[str]]:
    found: dict[int, list[str]] = {}
    for path in (ROOT / "backend" / "tests").rglob("test_*.py"):
        for match in re.finditer(
            r"@pytest\.mark\.scenario\((\d+)\)", path.read_text(encoding="utf-8")
        ):
            found.setdefault(int(match.group(1)), []).append(path.name)
    for path in (ROOT / "visual" / "test").rglob("*.test.ts*"):
        for match in re.finditer(r"//\s*@scenario\s+(\d+)", path.read_text(encoding="utf-8")):
            found.setdefault(int(match.group(1)), []).append(path.name)
    return found


def test_every_scenario_is_covered() -> None:
    missing = sorted(SCENARIOS - covered().keys())

    assert missing == [], f"security scenarios without a test: {missing}"


def test_no_unknown_scenario_numbers() -> None:
    assert covered().keys() <= SCENARIOS
