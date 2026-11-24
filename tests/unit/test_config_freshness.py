from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from autotrade.config import DEFAULT_RPC_URL, AppConfig, load_dotenv
from autotrade.domain.timeutil import fmt_ts
from autotrade.ingestion.freshness import Freshness, assess
from autotrade.observability.logging import redact
from tests.conftest import MINT, T0


def test_defaults_from_empty_env():
    c = AppConfig.from_env({})
    assert c.mode == "paper" and not c.live_enabled
    assert c.providers.rpc_url == DEFAULT_RPC_URL and c.data.db_path == Path(
        "data/autotrade.sqlite3"
    )
    assert c.data.watch_mints == ()


def test_env_overrides():
    c = AppConfig.from_env(
        {
            "SOLANA_RPC_URL": "https://rpc.example.test/key",
            "SOLANA_RPC_FALLBACK_URLS": "https://b.test, https://c.test",
            "AUTOTRADE_DB_PATH": "/tmp/x.db",
            "AUTOTRADE_MAX_ATTEMPTS": "5",
            "AUTOTRADE_WATCH_MINTS": MINT,
            "AUTOTRADE_OFFLINE": "true",
            "AUTOTRADE_LOG_LEVEL": "debug",
        }
    )
    assert c.providers.endpoints == [
        "https://rpc.example.test/key",
        "https://b.test",
        "https://c.test",
    ]
    assert c.providers.max_attempts == 5 and c.data.offline and c.data.log_level == "DEBUG"


@pytest.mark.parametrize(
    "env",
    [
        {"AUTOTRADE_MODE": "live"},
        {"AUTOTRADE_LIVE_ENABLED": "true"},
        {"AUTOTRADE_LIVE_ENABLED": "maybe"},
        {"SOLANA_RPC_URL": "ftp://x"},
        {"SOLANA_RPC_URL": "http://public.example.com"},
        {"AUTOTRADE_HTTP_TIMEOUT_SECONDS": "0"},
        {"AUTOTRADE_MAX_ATTEMPTS": "abc"},
        {"AUTOTRADE_WATCH_MINTS": "notanaddress"},
        {"AUTOTRADE_BACKOFF_BASE_SECONDS": "10", "AUTOTRADE_BACKOFF_MAX_SECONDS": "1"},
        {"AUTOTRADE_POLL_INTERVAL_SECONDS": "0.1"},
        {"SOLANA_RPC_COMMITMENT": "fast"},
    ],
)
def test_invalid_env_fails_closed(env):
    with pytest.raises((ValidationError, ValueError)):
        AppConfig.from_env(env)


def test_localhost_http_allowed():
    assert AppConfig.from_env({"SOLANA_RPC_URL": "http://127.0.0.1:8899"}).providers.rpc_url


def test_dotenv_does_not_override(tmp_path):
    f = tmp_path / ".env"
    f.write_text("# c\nA=1\nB='two'\nbad line\n")
    env = {"A": "keep"}
    assert load_dotenv(f, env) == 1 and env == {"A": "keep", "B": "two"}
    assert load_dotenv(tmp_path / "missing", env) == 0


def test_freshness_states():
    assert assess(None, T0, 30).state is Freshness.UNKNOWN
    assert assess(T0 - timedelta(seconds=10), T0, 30).state is Freshness.FRESH
    assert assess(T0 - timedelta(seconds=31), T0, 30).state is Freshness.STALE
    assert assess(T0 + timedelta(minutes=10), T0, 30).state is Freshness.UNKNOWN


def test_redaction():
    out = redact("failed https://rpc.test/v2/KEY?api-key=abc and token=xyz")
    assert "KEY" not in out and "abc" not in out and "xyz" not in out


def test_timestamp_format_sortable():
    assert fmt_ts(T0) < fmt_ts(T0 + timedelta(microseconds=1))
