import pytest

from openbot.printer import Printer
from openbot.printer.auth import FileCredentialStore
from openbot.printer.trust import TrustStore

from fake_printer import FakePrinter


@pytest.fixture
async def fake():
    fp = await FakePrinter().start()
    yield fp
    await fp.stop()


@pytest.fixture
def stores(tmp_path):
    return TrustStore(str(tmp_path)), FileCredentialStore(str(tmp_path))


@pytest.fixture
def make_printer(fake, stores):
    trust, creds = stores

    def factory():
        return Printer("127.0.0.1", port=fake.port, trust=trust, credentials=creds)
    return factory


@pytest.fixture
async def paired(fake, make_printer):
    """A connected, paired, trusted Printer."""
    p = make_printer()
    await p.hello()
    p.trust.trust(p.info.serial, await p.fetch_certificate())
    await p.pair("OpenBot@test")
    yield p
    await p.close()
