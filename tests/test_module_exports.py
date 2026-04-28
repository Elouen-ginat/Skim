from __future__ import annotations

from skaal.agent import Agent
from skaal.channel import Channel
from skaal.module import Module
from skaal.storage import Store


def test_module_export_groups_symbols_by_bucket() -> None:
    auth = Module("auth")

    @auth.storage()
    class Sessions(Store[dict]):
        pass

    @auth.agent()
    class User(Agent):
        pass

    @auth.function()
    async def lookup() -> dict[str, bool]:
        return {"ok": True}

    @auth.channel()
    class Events(Channel[dict]):
        pass

    exports = auth.export(Sessions, User, lookup, Events)

    assert exports.storage == {"Sessions": Sessions}
    assert exports.agents == {"User": User}
    assert exports.functions == {"lookup": lookup}
    assert exports.channels == {"Events": Events}
