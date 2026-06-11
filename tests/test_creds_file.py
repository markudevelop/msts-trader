from __future__ import annotations

import json

import pytest

from msts_trader.creds_file import CredsFileError, load_into_env, parse


def test_parse_json_canonical_keys():
    out = parse(json.dumps({"TT_PROVIDER_SECRET": "a", "TT_REFRESH_TOKEN": "b"}))
    assert out == {"TT_PROVIDER_SECRET": "a", "TT_REFRESH_TOKEN": "b"}


def test_parse_json_lowercase_aliases():
    out = parse(json.dumps({"provider_secret": "a", "refresh_token": "b", "account_id": "c"}))
    assert out == {"TT_PROVIDER_SECRET": "a", "TT_REFRESH_TOKEN": "b", "TT_ACCOUNT_ID": "c"}


def test_parse_dotenv():
    text = "# comment\nTT_PROVIDER_SECRET=abc\n\nTT_REFRESH_TOKEN=def\n"
    out = parse(text)
    assert out == {"TT_PROVIDER_SECRET": "abc", "TT_REFRESH_TOKEN": "def"}


def test_parse_dotenv_strips_quotes():
    out = parse('TT_PROVIDER_SECRET="quoted-value"\n')
    assert out == {"TT_PROVIDER_SECRET": "quoted-value"}


def test_parse_dotenv_value_with_equals():
    out = parse("TT_REFRESH_TOKEN=abc=def==\n")
    assert out == {"TT_REFRESH_TOKEN": "abc=def=="}


def test_parse_dotenv_aliases():
    out = parse("api_key=K\nsecret_key=S\npaper=true\n")
    assert out == {"APCA_API_KEY_ID": "K", "APCA_API_SECRET_KEY": "S", "APCA_PAPER": "true"}


def test_parse_client_secret_alias():
    # tastytrade's developer portal labels the provider secret "client secret"
    out = parse("client_secret=cs\nrefresh_token=rt\n")
    assert out == {"TT_PROVIDER_SECRET": "cs", "TT_REFRESH_TOKEN": "rt"}


def test_parse_is_test_alias():
    out = parse(json.dumps({"is_test": "1"}))
    assert out == {"TT_TEST": "1"}


def test_parse_empty_raises():
    with pytest.raises(CredsFileError, match="empty"):
        parse("   \n  ")


def test_parse_bad_json_raises():
    with pytest.raises(CredsFileError, match="invalid JSON"):
        parse("{not valid json")


def test_parse_json_non_object_raises():
    with pytest.raises(CredsFileError, match="must be an object"):
        parse("[1, 2, 3]")


def test_parse_dotenv_missing_equals_raises():
    with pytest.raises(CredsFileError, match="expected KEY=VALUE"):
        parse("JUST_A_KEY\n")


def test_parse_dotenv_empty_key_raises():
    with pytest.raises(CredsFileError, match="empty key"):
        parse("=value\n")


def test_parse_only_comments_raises():
    with pytest.raises(CredsFileError, match="no key/value pairs"):
        parse("# just a comment\n# another\n")


def test_broker_kwargs_from_file_missing_raises():
    from msts_trader.creds_file import broker_kwargs_from_file

    with pytest.raises(CredsFileError, match="not found"):
        broker_kwargs_from_file("tastytrade", "/no/such/creds.env")


def test_load_into_env_sets_vars(tmp_path, monkeypatch):
    monkeypatch.delenv("TT_PROVIDER_SECRET", raising=False)
    monkeypatch.delenv("TT_REFRESH_TOKEN", raising=False)
    f = tmp_path / "creds.json"
    f.write_text(json.dumps({"TT_PROVIDER_SECRET": "x", "TT_REFRESH_TOKEN": "y"}))
    keys = load_into_env(f)
    assert set(keys) == {"TT_PROVIDER_SECRET", "TT_REFRESH_TOKEN"}
    import os
    assert os.environ["TT_PROVIDER_SECRET"] == "x"
    assert os.environ["TT_REFRESH_TOKEN"] == "y"


def test_load_into_env_does_not_overwrite_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("TT_PROVIDER_SECRET", "already-set")
    f = tmp_path / "creds.json"
    f.write_text(json.dumps({"TT_PROVIDER_SECRET": "from-file"}))
    keys = load_into_env(f)
    assert keys == []  # nothing set because env already had it
    import os
    assert os.environ["TT_PROVIDER_SECRET"] == "already-set"


def test_load_into_env_overwrite(tmp_path, monkeypatch):
    monkeypatch.setenv("TT_PROVIDER_SECRET", "already-set")
    f = tmp_path / "creds.json"
    f.write_text(json.dumps({"TT_PROVIDER_SECRET": "from-file"}))
    load_into_env(f, overwrite=True)
    import os
    assert os.environ["TT_PROVIDER_SECRET"] == "from-file"


def test_load_into_env_missing_file_raises():
    with pytest.raises(CredsFileError, match="not found"):
        load_into_env("/nonexistent/path/creds.json")
