"""Cross-cutting consistency: every broker in SUPPORTED must be fully wired.

Prevents the class of bug where a broker is added to one registry but not
another (e.g. in the factory but missing a login flow — which is exactly
what shipped broken for Hyperliquid in 0.3.x).
"""
from __future__ import annotations

import pytest

from msts_trader.__main__ import _LOGIN_FLOWS
from msts_trader.brokers import SUPPORTED, BrokerError, make
from msts_trader.creds_file import broker_kwargs


def test_login_flows_cover_all_brokers():
    assert set(_LOGIN_FLOWS) == set(SUPPORTED)


@pytest.mark.parametrize("name", SUPPORTED)
def test_factory_knows_every_broker(name):
    # make() with no creds should raise a *credential/argument* error,
    # never "unknown broker" — i.e. the factory has a branch for it.
    try:
        make(name)
    except BrokerError as e:
        assert "unknown broker" not in str(e).lower(), f"{name} missing from factory"
    except TypeError:
        pass  # missing required kwargs is fine — the branch exists


@pytest.mark.parametrize("name", SUPPORTED)
def test_env_creds_handles_every_broker(name):
    # broker_kwargs must return None (not crash) for an unconfigured broker.
    assert broker_kwargs(name, lambda _k: None) is None


def test_no_unknown_broker_in_supported():
    # make() on a genuinely unknown name still raises the unknown-broker error.
    with pytest.raises(BrokerError, match="unknown broker"):
        make("definitely-not-a-broker")
