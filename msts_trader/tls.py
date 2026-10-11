"""Make every HTTP client trust the Windows certificate store.

Python's own `ssl`/`urllib` already trusts the Windows store, but `requests`
(and the broker SDKs on it) and `curl_cffi` (yfinance's HTTP client) trust
only the bundled `certifi` list. Antivirus HTTPS scanning (Avast/AVG Web
Shield, Kaspersky, ESET) and corporate proxies re-sign every site with a root
they add to the Windows store, so those clients fail with "certificate verify
failed" — which yfinance reports as "no price data ... possibly delisted".

`use_system_trust()` writes certifi + the Windows trusted roots to one PEM
file and points SSL_CERT_FILE / REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE at it.
curl_cffi reads those once at import, so this must run before anything
imports yfinance. A bundle the user configured themselves is left alone.
"""

from __future__ import annotations

import os
import ssl
import sys
from pathlib import Path

ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
_SERVER_AUTH = "1.3.6.1.5.5.7.3.1"


def _windows_roots() -> list[str]:
    out = []
    for der, enc, trust in ssl.enum_certificates("ROOT"):  # type: ignore[attr-defined]
        if enc == "x509_asn" and (trust is True or _SERVER_AUTH in trust):
            out.append(ssl.DER_cert_to_PEM_cert(der))
    return out


def use_system_trust(path: Path | None = None) -> Path | None:
    """Returns the bundle path it set, or None when it changed nothing."""
    if sys.platform != "win32" or any(os.environ.get(v) for v in ENV_VARS):
        return None
    try:
        import certifi

        base = Path(certifi.where()).read_text(encoding="ascii")
        roots = _windows_roots()
    except Exception:
        return None
    if not roots:
        return None
    path = path or Path(os.path.expanduser("~/.msts-trader/ca-bundle.pem"))
    pem = base.rstrip("\n") + "\n\n# Windows trusted roots\n" + "".join(roots)
    try:
        if not path.exists() or path.read_text(encoding="ascii") != pem:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(pem, encoding="ascii")
            os.replace(tmp, path)
    except OSError:
        return None
    for v in ENV_VARS:
        os.environ[v] = str(path)
    return path
