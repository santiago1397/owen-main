"""Pure structural validator for a call-flow graph.

Design (from the BulkVS+Asterisk platform spec, Ticket 02):
- A flow's `graph` is a true directed graph: `nodes` is an object map keyed by node id;
  edges live in each node's `next` map keyed by PORT (a menu node's ports are the DTMF
  digits; an `hours` node's ports are open/closed; etc.).
- `record` is a MODIFIER on a node (a `record: true` flag), never its own node type, so
  it is simply ignored here — we only look at `type` and `next`.
- A flow-level `default_fallback` node reference is where unwired / errored ports fall
  through at runtime, so validation treats missing ports as WARNINGS, not errors.

`validate_graph` is a PURE function (graph dict in -> ValidationResult out) with NO DB /
engine imports, so it can be unit-tested in isolation (mirrors analysis.audio.merge_channels).

Validation GATES ACTIVATION, not draft saving:
- HARD ERRORS (block activation): exactly one `entry` node; every edge target resolves to
  an existing node; every port is type-correct for its node type (plus: known node type,
  well-formed shape, resolvable `default_fallback`).
- WARNINGS (do NOT block): unreachable nodes; unwired expected ports; cycles.
"""

from dataclasses import dataclass, field

# The node types. `record` is deliberately absent — it is a node modifier. Ticket 17 adds
# the parity nodes: set_vars / unset_vars / conditions / send_sms / request.
NODE_TYPES: frozenset[str] = frozenset(
    {
        "entry", "play", "hours", "menu", "dial", "voicemail", "ai_agent", "hangup",
        "set_vars", "unset_vars", "conditions", "send_sms", "request",
    }
)

_MENU_PORTS: frozenset[str] = frozenset(
    {str(d) for d in range(10)} | {"*", "#", "timeout", "invalid", "default"}
)

# Ports that are type-correct for each node type. A port key outside this set is a HARD
# ERROR ("ports are type-correct for the node type"). `hangup` is terminal: no ports.
# `conditions` is DYNAMIC (each row's port + "else" — like menu digits): see
# condition_ports(), applied in validate_graph / _warn_unwired_ports.
ALLOWED_PORTS: dict[str, frozenset[str]] = {
    "entry": frozenset({"default"}),
    "play": frozenset({"default"}),
    "hours": frozenset({"open", "closed"}),
    "menu": _MENU_PORTS,
    "dial": frozenset({"answered", "noanswer", "busy", "failed"}),
    "voicemail": frozenset({"default"}),
    # Ticket 15.4: aligned with the engine's exit vocabulary — the engine's "end_call"
    # result is mapped to the "complete" port at the interpreter seam.
    "ai_agent": frozenset({"default", "transfer", "complete", "failed"}),
    "hangup": frozenset(),
    # Ticket 17 parity nodes.
    "set_vars": frozenset({"default"}),
    "unset_vars": frozenset({"default"}),
    "send_sms": frozenset({"default"}),
    "request": frozenset({"success", "failure"}),
    "conditions": frozenset(),  # placeholder — replaced per-node by condition_ports()
}

# Ports we EXPECT to be wired for a node type; a missing one is a WARNING (the
# default_fallback catches it at runtime). Types with dynamic/optional wiring
# (menu, voicemail, hangup) have no expected ports; `conditions` expectations are
# dynamic (each row's port + the required "else") — see _warn_unwired_ports.
EXPECTED_PORTS: dict[str, frozenset[str]] = {
    "entry": frozenset({"default"}),
    "play": frozenset({"default"}),
    "hours": frozenset({"open", "closed"}),
    "dial": frozenset({"answered"}),
    "ai_agent": frozenset({"default", "transfer", "complete", "failed"}),
    "set_vars": frozenset({"default"}),
    "unset_vars": frozenset({"default"}),
    "send_sms": frozenset({"default"}),
    "request": frozenset({"success", "failure"}),
}


def condition_ports(node: dict) -> frozenset[str]:
    """The dynamic port set of a `conditions` node: every configured row's port + "else"
    (the required no-match exit). Malformed rows are ignored (the runtime skips them too)."""
    ports = {"else"}
    rows = node.get("rows")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("port"):
                ports.add(str(row["port"]))
    return frozenset(ports)


@dataclass
class ValidationResult:
    """Outcome of validating a graph. `ok` is what ACTIVATION checks."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """A graph may be activated iff it has no hard errors. Warnings do not block."""
        return not self.errors


def _edges(node: dict) -> dict:
    """A node's `next` map, defensively coerced to a dict ({} if absent)."""
    nxt = node.get("next")
    return nxt if isinstance(nxt, dict) else {}


def validate_graph(graph: dict) -> ValidationResult:
    """Validate a call-flow graph. Pure: no side effects, no I/O.

    Returns a ValidationResult with separated `errors` (block activation) and
    `warnings` (reported but allow activation).
    """
    result = ValidationResult()

    if not isinstance(graph, dict):
        result.errors.append("graph must be an object")
        return result

    nodes = graph.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        result.errors.append("graph has no nodes")
        return result

    # Per-node shape guard so later passes can assume dict nodes with a dict `next`.
    for nid, node in nodes.items():
        if not isinstance(node, dict):
            result.errors.append(f"node '{nid}' must be an object")
    if result.errors:
        return result

    # --- HARD ERROR: exactly one entry node ---
    entries = [nid for nid, n in nodes.items() if n.get("type") == "entry"]
    if len(entries) == 0:
        result.errors.append("graph has no entry node (exactly one required)")
    elif len(entries) > 1:
        joined = ", ".join(sorted(entries))
        result.errors.append(f"graph has {len(entries)} entry nodes (exactly one required): {joined}")

    # --- HARD ERRORS: known type, type-correct ports, resolvable edge targets ---
    for nid, node in nodes.items():
        ntype = node.get("type")
        edges = _edges(node)

        if ntype not in NODE_TYPES:
            result.errors.append(f"node '{nid}' has unknown type '{ntype}'")
            # Skip port checks for unknown types; still resolve targets below.
        else:
            allowed = condition_ports(node) if ntype == "conditions" else ALLOWED_PORTS[ntype]
            for port in edges:
                if port not in allowed:
                    result.errors.append(
                        f"node '{nid}' ({ntype}) has invalid port '{port}'"
                    )

        for port, target in edges.items():
            if target not in nodes:
                result.errors.append(
                    f"node '{nid}' port '{port}' points to unknown node '{target}'"
                )

    # --- HARD ERROR: default_fallback must resolve if set ---
    fallback = graph.get("default_fallback")
    if fallback is not None and fallback not in nodes:
        result.errors.append(f"default_fallback points to unknown node '{fallback}'")

    # --- WARNINGS ---
    _warn_unwired_ports(nodes, result)
    _warn_unreachable(nodes, entries, fallback, result)
    _warn_cycles(nodes, result)

    return result


def _warn_unwired_ports(nodes: dict, result: ValidationResult) -> None:
    for nid, node in nodes.items():
        ntype = node.get("type")
        edges = _edges(node)
        if ntype == "menu" and not edges:
            result.warnings.append(f"menu node '{nid}' has no options wired")
            continue
        expected = (
            condition_ports(node) if ntype == "conditions"
            else EXPECTED_PORTS.get(ntype, frozenset())
        )
        for port in sorted(expected):
            if port not in edges:
                result.warnings.append(f"node '{nid}' ({ntype}) has unwired port '{port}'")


def _warn_unreachable(
    nodes: dict, entries: list[str], fallback: object, result: ValidationResult
) -> None:
    # Seed reachability from the entry node(s) AND the default_fallback (the fallback is a
    # legitimate runtime destination, so nodes reachable only via it are not "unreachable").
    seeds = [e for e in entries]
    if isinstance(fallback, str) and fallback in nodes:
        seeds.append(fallback)

    seen: set[str] = set()
    stack = list(seeds)
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for target in _edges(nodes[cur]).values():
            if target in nodes and target not in seen:
                stack.append(target)

    for nid in sorted(nodes):
        if nid not in seen:
            result.warnings.append(f"node '{nid}' is unreachable")


def _warn_cycles(nodes: dict, result: ValidationResult) -> None:
    # DFS with a recursion stack; report the first back-edge cycle found (as a warning).
    WHITE, GREY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in nodes}

    def visit(nid: str, path: list[str]) -> list[str] | None:
        color[nid] = GREY
        path.append(nid)
        for target in _edges(nodes[nid]).values():
            if target not in nodes:
                continue
            if color[target] == GREY:
                cycle = path[path.index(target):] + [target]
                return cycle
            if color[target] == WHITE:
                found = visit(target, path)
                if found is not None:
                    return found
        path.pop()
        color[nid] = BLACK
        return None

    for nid in nodes:
        if color[nid] == WHITE:
            cycle = visit(nid, [])
            if cycle is not None:
                result.warnings.append("graph contains a cycle: " + " -> ".join(cycle))
                return
