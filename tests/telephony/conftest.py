import asyncio

import pytest

from chaarteeke.telephony import AriAdapter
from chaarteeke.telephony.fake_ari import PASSWORD, USER, FakeAri


@pytest.fixture
def run():
    return asyncio.run


class Rig:
    def __init__(self, fake, adapter):
        self.fake, self.adapter = fake, adapter
        self._it = adapter.events()

    async def next(self, timeout=2):
        return await asyncio.wait_for(self._it.__anext__(), timeout)


async def make_rig(**kw):
    fake = FakeAri()
    url = await fake.start()
    adapter = AriAdapter(
        url, USER, PASSWORD, app="chaar", endpoint_template=kw.get("tpl", "Quectel/quectel0/{number}")
    )
    await adapter.start()
    return Rig(fake, adapter)


async def close_rig(rig):
    await rig.adapter.stop()
    await rig.fake.stop()
