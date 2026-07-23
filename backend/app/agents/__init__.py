"""AI voice-agent domain (Ticket 11): the VoiceAgentSession seam + tool registry + the
append-only version service. Mirrors app/flows and app/analysis/transcription.

Intentionally free of DB/engine-runtime imports (stdlib + app.core.config only) so the seam,
tools, and version kernel are unit-testable in the sandbox. The DB glue that runs a session
against a live ARI call lives in app/flows/runtime.py.
"""

from app.agents.service import build_spec, next_version_number
from app.agents.session import (
    AgentCallContext,
    AgentResult,
    AgentSpec,
    DummyVoiceAgentSession,
    VoiceAgentSession,
    get_session_for_agent,
    get_voice_agent_session,
    select_voice_agent_engine,
)
from app.agents.tools import TOOLS, VALID_PORTS, enabled_tools, is_valid_port

__all__ = [
    "AgentCallContext",
    "AgentResult",
    "AgentSpec",
    "DummyVoiceAgentSession",
    "VoiceAgentSession",
    "get_session_for_agent",
    "get_voice_agent_session",
    "select_voice_agent_engine",
    "TOOLS",
    "VALID_PORTS",
    "enabled_tools",
    "is_valid_port",
    "build_spec",
    "next_version_number",
]
