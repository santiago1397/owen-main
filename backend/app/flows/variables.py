"""Per-call flow variables: {{var}} interpolation + conditions evaluation (Ticket 17).

Pure and STDLIB-ONLY (mirrors app/flows/interpreter.py / validator.py) so every rule here is
unit-testable in the sandbox (tests/test_flow_variables.py). The variable STORE itself is a
plain dict owned by the FlowInterpreter (one per call); this module only defines how values
are resolved, substituted into text, and compared.

Resolution rules:
- A variable name is looked up as a DIRECT key first ("caller_number", "call.time",
  "gather.digits" are all direct keys — dots in a built-in's name do not imply nesting).
- Otherwise the name is resolved as a DOT-PATH: the longest key prefix present in the store
  anchors the walk, and the remaining segments traverse nested dicts/lists (list segments
  must be integer indices). E.g. "request.body.data.status" anchors at the "request.body"
  key (the parsed JSON of the last request node) and walks ["data", "status"].
- An unresolvable name yields None — which interpolates to the EMPTY STRING (spec: unknown
  vars interpolate to empty) and compares as "".

Conditions (`conditions` node): ordered rows {variable, operator, value, port}; FIRST match
wins; no match -> the caller takes the "else" port. Operators: equals, not_equals, contains,
regex, gt, lt, is_empty. gt/lt compare NUMERICALLY when both sides parse as numbers, else
lexicographically. A row with a bad regex (or malformed shape) is SKIPPED with a log — never
raised. There is NO eval/exec anywhere, ever.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger("flows.variables")

# {{ name }} — non-greedy, whitespace-tolerant inside the braces.
_TEMPLATE_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")

CONDITION_OPERATORS: frozenset[str] = frozenset(
    {"equals", "not_equals", "contains", "regex", "gt", "lt", "is_empty"}
)


def resolve_var(variables: dict, name: str) -> Any:
    """Resolve `name` against the store: direct key first, then dot-path (see module doc).

    Returns None when the name cannot be resolved (unknown var / path off the data)."""
    if not isinstance(variables, dict) or not name:
        return None
    name = str(name)
    if name in variables:
        return variables[name]
    parts = name.split(".")
    # Longest anchoring prefix wins (so "request.body.data.status" anchors at "request.body"
    # even though "request" is not itself a key).
    for i in range(len(parts) - 1, 0, -1):
        prefix = ".".join(parts[:i])
        if prefix in variables:
            return _walk(variables[prefix], parts[i:])
    return None


def _walk(value: Any, path: list[str]) -> Any:
    """Follow `path` segments into nested dicts/lists; None as soon as a step fails."""
    cur = value
    for seg in path:
        if isinstance(cur, dict):
            if seg in cur:
                cur = cur[seg]
                continue
            return None
        if isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(seg)]
                continue
            except (ValueError, IndexError):
                return None
        return None
    return cur


def interpolate(text: Any, variables: dict) -> str:
    """Substitute every {{var}} in `text` with its resolved value (str()'d).

    Unknown / unresolvable vars become the EMPTY string. Non-string `text` is str()'d
    first (None -> ""). Always returns a str; never raises."""
    if text is None:
        return ""
    text = text if isinstance(text, str) else str(text)

    def _sub(m: re.Match) -> str:
        value = resolve_var(variables, m.group(1))
        return "" if value is None else str(value)

    return _TEMPLATE_RE.sub(_sub, text)


# --- conditions evaluation ----------------------------------------------------------------

def _as_number(value: Any) -> Optional[float]:
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return None


def _matches(operator: str, actual: str, expected: str) -> bool:
    """One row's comparison. `actual`/`expected` are already coerced to strings (missing
    variable -> ""). Raises re.error only for `regex` — the caller skips that row."""
    if operator == "equals":
        return actual == expected
    if operator == "not_equals":
        return actual != expected
    if operator == "contains":
        return expected in actual
    if operator == "regex":
        return re.search(expected, actual) is not None
    if operator == "is_empty":
        return actual == ""
    if operator in ("gt", "lt"):
        a_num, e_num = _as_number(actual), _as_number(expected)
        if a_num is not None and e_num is not None:
            return a_num > e_num if operator == "gt" else a_num < e_num
        return actual > expected if operator == "gt" else actual < expected
    return False  # unknown operator -> row does not match (validator catches it earlier)


def evaluate_conditions(
    rows: list, variables: dict
) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """Evaluate ordered condition rows; FIRST match wins.

    Returns (row_index, port, actual_value) of the matching row, or (None, None, None) when
    nothing matched (the caller routes to "else"). Malformed rows and bad regexes are
    SKIPPED with a log — evaluation never raises."""
    if not isinstance(rows, list):
        return None, None, None
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        operator = str(row.get("operator") or "")
        port = row.get("port")
        variable = row.get("variable")
        if operator not in CONDITION_OPERATORS or not port or not variable:
            logger.warning("conditions: skipping malformed row %d (%r)", idx, row)
            continue
        resolved = resolve_var(variables, str(variable))
        actual = "" if resolved is None else str(resolved)
        expected = interpolate(row.get("value"), variables)
        try:
            if _matches(operator, actual, expected):
                return idx, str(port), actual
        except re.error:
            logger.warning("conditions: bad regex in row %d (%r); row skipped", idx, row.get("value"))
            continue
    return None, None, None
