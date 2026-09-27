import pytest

from openbot.printer.errors import CertificateChanged, NotTrusted
from openbot.printer.trust import CertInfo, TrustStore

from fake_printer import SERIAL, make_cert

REAL_PEM = open("probe_out/printer_cert.pem").read() if __import__("os").path.exists(
    "probe_out/printer_cert.pem") else None


def _cert():
    return CertInfo.from_pem(make_cert()[0].decode())


def test_trust_roundtrip(tmp_path):
    store = TrustStore(str(tmp_path))
    c = _cert()
    assert store.get(SERIAL) is None
    store.trust(SERIAL, c)
    assert store.get(SERIAL).sha256 == c.sha256
    assert list(store.all()) == [SERIAL]
    store.remove(SERIAL)
    assert store.get(SERIAL) is None


def test_context_for_detects_untrusted_and_changed(tmp_path):
    store = TrustStore(str(tmp_path))
    a, b = _cert(), _cert()
    with pytest.raises(NotTrusted):
        store.context_for(SERIAL, presented=a)
    store.trust(SERIAL, a)
    store.context_for(SERIAL, presented=a)
    with pytest.raises(CertificateChanged):
        store.context_for(SERIAL, presented=b)


def test_import_export_pem_and_der(tmp_path):
    import ssl
    store = TrustStore(str(tmp_path))
    c = _cert()
    pem = tmp_path / "c.pem"
    pem.write_text(c.pem)
    assert store.import_file(SERIAL, str(pem)).sha256 == c.sha256
    der = tmp_path / "c.cer"
    der.write_bytes(ssl.PEM_cert_to_DER_cert(c.pem))
    assert store.import_file("OTHER1", str(der)).sha256 == c.sha256
    out = tmp_path / "out.pem"
    store.export_file(SERIAL, str(out))
    assert CertInfo.from_pem(out.read_text()).sha256 == c.sha256


def test_serial_is_sanitized(tmp_path):
    store = TrustStore(str(tmp_path))
    store.trust("../../evil", _cert())
    assert (tmp_path / "trust" / "evil.pem").exists()


@pytest.mark.skipif(REAL_PEM is None, reason="no captured printer certificate")
def test_real_printer_certificate_details():
    c = CertInfo.from_pem(REAL_PEM)
    assert "CN=MakerBot Replicator" in c.subject
    assert c.self_signed and c.is_ca
    assert c.fingerprint_display.startswith("AE:60:D1:21")
    assert c.not_after.year == 4754
