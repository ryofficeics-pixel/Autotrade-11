import asyncio

import pytest

from autotrade.data.providers.rpc_adapters import RpcTokenMetadataProvider
from autotrade.data.providers.token_ext import metadata_names, normalize_extensions
from autotrade.data.storage import repositories as repo
from tests.conftest import MINT, WALLET, make_rpc, mint_account, rpc_transport

run = asyncio.run
# Shape captured live from a Token-2022 mainnet mint (metadataPointer + tokenMetadata).
LIVE = [
    {"extension": "metadataPointer", "state": {"authority": None, "metadataAddress": MINT}},
    {
        "extension": "tokenMetadata",
        "state": {
            "additionalMetadata": [],
            "mint": MINT,
            "name": "Flywheel",
            "symbol": "FLYWHEEL",
            "updateAuthority": None,
            "uri": "https://x.test/m",
        },
    },
]
# Shape per the Solana jsonParsed docs for fee/delegate/hook extensions (not a live capture).
DOCS = [
    {
        "extension": "transferFeeConfig",
        "state": {
            "newerTransferFee": {"epoch": 9, "maximumFee": 5, "transferFeeBasisPoints": 250},
            "olderTransferFee": {"epoch": 1, "maximumFee": 1, "transferFeeBasisPoints": 10},
            "transferFeeConfigAuthority": WALLET,
            "withdrawWithheldAuthority": None,
            "withheldAmount": 0,
        },
    },
    {"extension": "permanentDelegate", "state": {"delegate": WALLET}},
    {"extension": "transferHook", "state": {"authority": None, "programId": WALLET}},
    {"extension": "defaultAccountState", "state": {"accountState": "frozen"}},
    {"extension": "pausableConfig", "state": {"authority": WALLET}},
]


def test_live_shape_normalises_and_exposes_onchain_names():
    exts = normalize_extensions(LIVE)
    assert [e.name for e in exts] == ["metadataPointer", "tokenMetadata"]
    assert metadata_names(LIVE) == ("FLYWHEEL", "Flywheel")
    assert metadata_names([]) == (None, None) and metadata_names(None) == (None, None)


def test_docs_shape_keeps_only_analysed_fields():
    d = {e.name: e.detail for e in normalize_extensions(DOCS)}
    assert d["transferFeeConfig"] == {"fee_bps": "250", "config_authority": WALLET}  # larger fee
    assert d["permanentDelegate"] == {"delegate": WALLET}
    assert d["transferHook"]["program_id"] == WALLET
    assert d["defaultAccountState"] == {"state": "frozen"}
    assert d["pausableConfig"] == {"authority": WALLET}


@pytest.mark.parametrize(
    "bad", ["x", {"extension": "a"}, [1], [{"extension": 5}], [{"extension": "a", "state": "no"}]]
)
def test_malformed_extension_data_raises(bad):
    with pytest.raises(ValueError):
        normalize_extensions(bad)


def token2022_account(extensions):
    acct = mint_account(program="spl-token-2022")
    if extensions is not None:
        acct["data"]["parsed"]["info"]["extensions"] = extensions
    acct["owner"] = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
    return acct


def test_provider_parses_token_2022_extensions_and_names(clock, sleeper):
    rpc = make_rpc(
        rpc_transport(lambda m, p: {"context": {"slot": 1}, "value": token2022_account(LIVE)}),
        clock,
        sleeper,
    )
    t = run(RpcTokenMetadataProvider(rpc).get_token(MINT)).items[0]
    assert t.symbol == "FLYWHEEL" and t.name == "Flywheel"
    assert [e.name for e in t.extensions] == ["metadataPointer", "tokenMetadata"]
    assert t.token_program == "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


def test_mint_without_extensions_is_analysed_as_empty_not_unknown(clock, sleeper):
    rpc = make_rpc(
        rpc_transport(lambda m, p: {"context": {"slot": 1}, "value": token2022_account(None)}),
        clock,
        sleeper,
    )
    assert run(RpcTokenMetadataProvider(rpc).get_token(MINT)).items[0].extensions == ()


def test_malformed_extensions_reject_the_record(clock, sleeper):
    rpc = make_rpc(
        rpc_transport(lambda m, p: {"context": {"slot": 1}, "value": token2022_account("garbage")}),
        clock,
        sleeper,
    )
    res = run(RpcTokenMetadataProvider(rpc).get_token(MINT))
    assert res.items == [] and "extensions" in res.rejections[0]


def test_extensions_persist_and_update(db, clock, sleeper):
    rpc = make_rpc(
        rpc_transport(lambda m, p: {"context": {"slot": 1}, "value": token2022_account(DOCS)}),
        clock,
        sleeper,
    )
    t = run(RpcTokenMetadataProvider(rpc).get_token(MINT)).items[0]
    with db.session() as conn, db.transaction(conn):
        assert repo.upsert_token(conn, t) is repo.Outcome.INSERTED
        row = conn.execute("SELECT extensions FROM tokens").fetchone()
    assert "permanentDelegate" in row["extensions"]
    clock.advance(30)
    rpc2 = make_rpc(
        rpc_transport(lambda m, p: {"context": {"slot": 1}, "value": token2022_account(LIVE)}),
        clock,
        sleeper,
    )
    t2 = run(RpcTokenMetadataProvider(rpc2).get_token(MINT)).items[0]
    with db.session() as conn, db.transaction(conn):
        assert repo.upsert_token(conn, t2) is repo.Outcome.UPDATED
        row = conn.execute("SELECT extensions, symbol FROM tokens").fetchone()
    assert "permanentDelegate" not in row["extensions"] and row["symbol"] == "FLYWHEEL"
