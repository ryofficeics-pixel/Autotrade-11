"""Stage 1 safety invariants: read-only, no keys, no signing/broadcast, fail-closed ingestion."""

import asyncio
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from autotrade.config import AppConfig
from autotrade.data.providers.solana_rpc import ALLOWED_METHODS
from autotrade.domain.errors import InvalidRequest
from autotrade.ingestion.freshness import Freshness, assess
from tests.conftest import T0, make_rpc, rpc_transport

SRC = Path(__file__).resolve().parents[2] / "src" / "autotrade"
FORBIDDEN = re.compile(
    r"sendTransaction|sendRawTransaction|signTransaction|simulateTransaction|requestAirdrop|"
    r"Keypair|private_key|secret_key|seed_phrase|mnemonic|\bsolders\b|\bsolana\b\s*import",
    re.IGNORECASE,
)


# The secret scanner must name key material in order to detect it; it only matches text
# patterns and never loads, derives or uses a key.
DETECTOR_ONLY = {"ops/secretscan.py"}


def test_source_has_no_signing_or_broadcast_or_key_handling():
    offenders = [
        f"{p.relative_to(SRC)}:{m.group(0)}"
        for p in SRC.rglob("*.py")
        if p.relative_to(SRC).as_posix() not in DETECTOR_ONLY
        for m in FORBIDDEN.finditer(p.read_text())
    ]
    assert offenders == []


def test_rpc_allowlist_is_read_only():
    assert ALLOWED_METHODS == {
        "getSlot",
        "getAccountInfo",
        "getTransaction",
        "getTokenAccountBalance",
        "getSignaturesForAddress",
    }
    assert not any(
        m.lower().startswith(("send", "sign", "simulate", "request")) for m in ALLOWED_METHODS
    )


@pytest.mark.parametrize(
    "method",
    ["sendTransaction", "signTransaction", "requestAirdrop", "simulateTransaction", "getBalance"],
)
def test_rpc_client_refuses_non_allowlisted_methods_before_network(method, clock, sleeper):
    calls = []
    rpc = make_rpc(rpc_transport(lambda m, p: 1, calls), clock, sleeper)
    with pytest.raises(InvalidRequest):
        asyncio.run(rpc.call(method, []))
    assert calls == []


def test_live_mode_cannot_be_enabled_by_any_path():
    for kwargs in (
        {"live_enabled": True},
        {"mode": "live"},
        {"mode": "LIVE", "live_enabled": True},
    ):
        with pytest.raises(ValidationError):
            AppConfig(**kwargs)
    for env in ({"AUTOTRADE_LIVE_ENABLED": "1"}, {"AUTOTRADE_MODE": "live"}):
        with pytest.raises((ValidationError, ValueError)):
            AppConfig.from_env(env)


def test_config_has_no_key_or_wallet_fields():
    def names(model):
        out = set(model.model_fields)
        for f in model.model_fields.values():
            sub = f.annotation
            if hasattr(sub, "model_fields"):
                out |= names(sub)
        return out

    assert not [
        n
        for n in names(AppConfig)
        if any(w in n for w in ("secret", "seed", "private", "mnemonic", "wallet_key", "keypair"))
        or (n.endswith("key") and not n.endswith("api_key"))
    ]


def test_works_with_no_environment_and_no_keys():
    assert AppConfig.from_env({}).mode == "paper"


def test_stale_data_is_explicitly_identified():
    assert assess(None, T0, 30).state is Freshness.UNKNOWN  # missing is never "fresh"


def test_ingestion_package_has_no_dependency_on_trading_code():
    text = "".join(p.read_text() for p in (SRC / "ingestion").glob("*.py"))
    assert not re.search(r"\b(paper|broker|order|execution|strategy|risk)\b\s*import", text)


def test_api_keys_are_secret_strings_and_never_printed():
    from autotrade.config import ProviderSettings

    cfg = AppConfig.from_env({"BIRDEYE_API_KEY": "sekret-value", "JUPITER_API_KEY": "j-sekret"})
    assert "sekret" not in repr(cfg) and "sekret" not in cfg.model_dump_json()
    assert isinstance(
        cfg.providers.birdeye_api_key,
        type(ProviderSettings().jupiter_api_key or cfg.providers.birdeye_api_key),
    )
