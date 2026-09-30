"""Well-known actor identities for system-emitted audit events."""

from __future__ import annotations

from uuid import UUID

SYSTEM_FEEDBACK_RESOLVER_ACTOR_ID = UUID("00000000-0000-0000-0000-000000000020")
"""Actor used by feedback_resolution worker dispatch.

Emitted with ``actor_kind="agent"`` because the worker stands in for the
real agent loop until Epic_1 AskService LLM integration lands.

The current Epic_0 data model stores actor IDs as text and has no dedicated
actors table, so this UUID is reserved here and documented for future seed
integration when the agent identity model is formalized.
"""
