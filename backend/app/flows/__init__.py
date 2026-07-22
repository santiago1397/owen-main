"""Call-flow graph domain: schema helpers, the pure activation validator, and the
append-only version service. See app/flows/validator.py for the graph shape.

This package is intentionally free of any DB / engine imports so the validator can be
unit-tested in isolation (mirrors app.analysis.audio.merge_channels being a pure fn).
"""

from app.flows.service import next_version_number
from app.flows.validator import (
    ALLOWED_PORTS,
    NODE_TYPES,
    ValidationResult,
    validate_graph,
)

__all__ = [
    "ALLOWED_PORTS",
    "NODE_TYPES",
    "ValidationResult",
    "validate_graph",
    "next_version_number",
]
