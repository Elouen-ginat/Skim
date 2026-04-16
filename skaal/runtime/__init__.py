"""skaal.runtime — runtime implementations.

Distributed execution will be provided by the Rust mesh (``mesh/``) integration
that replaces the old Python-only ``DistributedRuntime`` stub.  Until that
lands, :class:`LocalRuntime` is the only built-in runtime; third parties can
register their own runtime implementations via ``skaal.plugins``.
"""

from skaal.runtime.agent_registry import AgentRecord, AgentRegistry, AgentStatus
from skaal.runtime.channels import Channel, LocalChannel
from skaal.runtime.local import LocalRuntime
from skaal.runtime.state import InMemoryStateStore, StateStore

__all__ = [
    "AgentRecord",
    "AgentRegistry",
    "AgentStatus",
    "Channel",
    "InMemoryStateStore",
    "LocalChannel",
    "LocalRuntime",
    "StateStore",
]
