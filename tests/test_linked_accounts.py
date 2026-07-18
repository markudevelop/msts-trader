"""Same-login multi-account: matching helpers + per-broker list/use_account."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from msts_trader.brokers.base import BrokerError, LinkedAccount, resolve_linked_account


# ----- resolve_linked_account -------------------------------------------------


def _accts(*pairs: tuple[str, str | None]) -> list[LinkedAccount]:
    return [LinkedAccount(id=i, number=n) for i, n in pairs]


def test_resolve_exact_id():
    a = _accts(("HASH1", "11112222"), ("HASH2", "33334444"))
    assert resolve_linked_account(a, "HASH2").id == "HASH2"


def test_resolve_exact_number():
    a = _accts(("HASH1", "11112222"), ("HASH2", "33334444"))
    assert resolve_linked_account(a, "11112222").number == "11112222"


def test_resolve_last4():
    a = _accts(("HASH1", "11112222"), ("HASH2", "33334444"))
    assert resolve_linked_account(a, "2222").id == "HASH1"
    assert resolve_linked_account(a, "4444").id == "HASH2"


def test_resolve_ambiguous_last4():
    a = _accts(("HASH1", "11112222"), ("HASH2", "99992222"))
    with pytest.raises(BrokerError, match="ambiguous"):
        resolve_linked_account(a, "2222")


def test_resolve_no_match():
    a = _accts(("HASH1", "11112222"))
    with pytest.raises(BrokerError, match="no account matching"):
        resolve_linked_account(a, "0000")


def test_resolve_empty_identifier():
    with pytest.raises(BrokerError, match="empty"):
        resolve_linked_account(_accts(("A", "1")), "  ")


def test_linked_account_masked():
    assert LinkedAccount(id="HASH", number="12345678").masked == "…5678"
    assert LinkedAccount(id="ABCDEFGH", number=None).masked == "…EFGH"


# ----- Schwab -----------------------------------------------------------------


@pytest.fixture
def schwab_mod():
    pytest.importorskip("schwab")
    import msts_trader.brokers.schwab as m

    return m


def test_schwab_list_and_use_account(schwab_mod):
    from msts_trader.brokers.schwab import Schwab

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    payload = [
        {"accountNumber": "11112222", "hashValue": "HASHAAAA"},
        {"accountNumber": "33334444", "hashValue": "HASHBBBB"},
    ]
    b = Schwab.__new__(Schwab)
    b._client = SimpleNamespace(get_account_numbers=lambda: _Resp(payload))
    # Simulate post-init apply of first account
    b._apply_linked(b._load_linked_accounts()[0])

    linked = b.list_linked_accounts()
    assert len(linked) == 2
    assert linked[0].number == "11112222"
    assert linked[1].id == "HASHBBBB"

    b.use_account("4444")
    assert b.account_hash == "HASHBBBB"
    assert b._account_hash == "HASHBBBB"
    assert b.account_id == "…4444"

    b.use_account("HASHAAAA")
    assert b.account_hash == "HASHAAAA"

    with pytest.raises(BrokerError, match="no account matching"):
        b.use_account("0000")


def test_schwab_init_selects_by_number(schwab_mod, monkeypatch, tmp_path):
    from msts_trader.brokers.schwab import Schwab

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class _Client:
        def get_account_numbers(self):
            return _Resp(
                [
                    {"accountNumber": "11112222", "hashValue": "HASHAAAA"},
                    {"accountNumber": "33334444", "hashValue": "HASHBBBB"},
                ]
            )

    monkeypatch.setattr(schwab_mod, "_client_from_stored_token", lambda *a, **k: _Client())
    monkeypatch.setattr(schwab_mod, "TOKEN_PATH", tmp_path / "tok.json")

    b = Schwab("k", "s", account_hash="4444")
    assert b.account_hash == "HASHBBBB"
    assert b.account_id == "…4444"


# ----- Tradier ----------------------------------------------------------------


def test_tradier_list_and_use_account(monkeypatch):
    from msts_trader.brokers.tradier import Tradier

    b = Tradier.__new__(Tradier)
    b._token = "x"
    b._base = "https://sandbox.tradier.com"
    b._timeout = 5.0
    profile = {
        "profile": {
            "account": [
                {"account_number": "VA1111"},
                {"account_number": "VA2222"},
            ]
        }
    }
    monkeypatch.setattr(b, "_request", lambda method, path, params=None: profile)
    b.account_id = "VA1111"

    linked = b.list_linked_accounts()
    assert [a.id for a in linked] == ["VA1111", "VA2222"]
    b.use_account("2222")
    assert b.account_id == "VA2222"


# ----- Tastytrade -------------------------------------------------------------


def test_tastytrade_list_and_use_account():
    from msts_trader.brokers.tastytrade import Tastytrade

    a1 = SimpleNamespace(account_number="5W1111")
    a2 = SimpleNamespace(account_number="5W2222")
    b = Tastytrade.__new__(Tastytrade)
    b._sess = object()
    b._acct_objs = [a1, a2]
    b._apply_account_obj(a1)

    assert [a.id for a in b.list_linked_accounts()] == ["5W1111", "5W2222"]
    b.use_account("2222")
    assert b.account_id == "5W2222"
    assert b._acct is a2


# ----- IBKR -------------------------------------------------------------------


def test_ibkr_list_and_use_account():
    pytest.importorskip("ib_insync")
    from msts_trader.brokers.ibkr import IBKR

    b = IBKR.__new__(IBKR)
    b._managed = ["U1111111", "U2222222"]
    b.account_id = "U1111111"
    assert len(b.list_linked_accounts()) == 2
    b.use_account("2222")
    assert b.account_id == "U2222222"
    with pytest.raises(BrokerError, match="no account matching"):
        b.use_account("9999")


# ----- Single-account brokers -------------------------------------------------


def test_paper_list_and_use():
    from msts_trader.brokers.paper import Paper

    b = Paper.__new__(Paper)
    b.account_id = "PAPER"
    assert b.list_linked_accounts()[0].id == "PAPER"
    b.use_account("PAPER")
    with pytest.raises(BrokerError, match="no account matching"):
        b.use_account("OTHER")
