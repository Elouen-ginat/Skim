"""skaal.runtime — runtime implementations (local and distributed)."""

from skaal.runtime.agent_registry import AgentRecord, AgentRegistry, AgentStatus
from skaal.runtime.channels import Channel, LocalChannel
from skaal.runtime.distributed import DistributedRuntime
from skaal.runtime.local import LocalRuntime
from skaal.runtime.state import InMemoryStateStore, StateStore

__all__ = [
    "AgentRecord",
    "AgentRegistry",
    "AgentStatus",
    "Channel",
    "DistributedRuntime",
    "InMemoryStateStore",
    "LocalChannel",
    "LocalRuntime",
    "StateStore",
]
