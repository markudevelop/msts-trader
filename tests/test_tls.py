"""System trust bundle (msts_trader/tls.py) and the certificate-failure hint."""

from __future__ import annotations

import ssl
import sys

import pytest

from msts_trader import tls
from msts_trader.symphony.feeds import unreachable

real_use_system_trust = tls.use_system_trust  # conftest stubs the module attribute per test


def test_cert_failure_message_names_the_local_causes():
    msg = unreachable(
        "pnlportfolio", "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired"
    )
    assert "system clock" in msg and "antivirus" in msg and "SSL_CERT_FILE" in msg
    assert unreachable("pnlportfolio", "timed out") == "pnlportfolio unreachable (timed out)"


def test_user_configured_bundle_is_left_alone(monkeypatch, tmp_path):
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "mine.pem"))
    assert real_use_system_trust(tmp_path / "ours.pem") is None
    assert not (tmp_path / "ours.pem").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="reads the Windows certificate store")
def test_bundle_has_certifi_and_windows_roots(monkeypatch, tmp_path):
    for v in tls.ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    path = real_use_system_trust(tmp_path / "ca.pem")
    assert path == tmp_path / "ca.pem"
    import os

    assert all(os.environ[v] == str(path) for v in tls.ENV_VARS)
    ctx = ssl.create_default_context(cafile=str(path))  # parses as a valid bundle
    assert len(ctx.get_ca_certs()) > 100
    assert path.read_text(encoding="ascii").count("BEGIN CERTIFICATE") > len(tls._windows_roots())
