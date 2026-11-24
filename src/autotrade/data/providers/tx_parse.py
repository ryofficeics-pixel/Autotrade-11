"""Swap-level interpretation of a `getTransaction` result (encoding "json") for ONE wallet.

Method: net balance change of the wallet per mint (meta.pre/postTokenBalances filtered by
`owner`) plus native lamports (pre/postBalances at the wallet's account index, fee added back
when the wallet pays it). A non-quote token moving one way while a quote asset (SOL, wSOL, USDC,
USDT) moves the other way is a swap leg: token up = buy, token down = sell. Anything else is a
transfer. Token-for-token swaps and unexpected shapes are `unparsed` (never guessed).
"""

from __future__ import annotations

from typing import Any

from autotrade.domain.models import FlowLeg, FlowSide

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTE_MINTS = frozenset({WSOL, USDC, USDT})
NATIVE = "SOL"
# Minimum |delta| for a token quote leg to count as payment (rounding crumbs are not trades).
MIN_QUOTE_RAW = {WSOL: 10_000, USDC: 1_000, USDT: 1_000}
DUST_LAMPORTS = 1_000_000  # 0.001 SOL: below this a native move is rent/noise, not a quote leg


def _token_deltas(meta: dict[str, Any], wallet: str) -> tuple[dict[str, int], dict[str, int]]:
    pre: dict[tuple[str, int], int] = {}
    post: dict[tuple[str, int], int] = {}
    decimals: dict[str, int] = {}
    for key, bucket in (("preTokenBalances", pre), ("postTokenBalances", post)):
        for b in meta.get(key) or []:
            if b.get("owner") != wallet:
                continue
            ui = b["uiTokenAmount"]
            bucket[(b["mint"], b["accountIndex"])] = int(ui["amount"])
            decimals[b["mint"]] = int(ui["decimals"])
    deltas: dict[str, int] = {}
    for mint, idx in set(pre) | set(post):
        deltas[mint] = deltas.get(mint, 0) + post.get((mint, idx), 0) - pre.get((mint, idx), 0)
    return {m: d for m, d in deltas.items() if d != 0}, decimals


def _native_delta(tx: dict[str, Any], wallet: str) -> int:
    meta = tx["meta"]
    keys = list(tx["transaction"]["message"]["accountKeys"])
    loaded = meta.get("loadedAddresses") or {}
    keys += list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])
    if wallet not in keys:
        return 0
    idx = keys.index(wallet)
    delta = int(meta["postBalances"][idx]) - int(meta["preBalances"][idx])
    return delta + int(meta["fee"]) if idx == 0 else delta  # fee payer is account 0


def parse_wallet_flows(tx: dict[str, Any], wallet: str) -> tuple[str, list[FlowLeg]]:
    """Return (kind, legs). kind in swap | transfer | failed | unparsed."""
    try:
        meta = tx["meta"]
        if meta is None:
            return "unparsed", []
        if meta.get("err") is not None:
            return "failed", []
        token, decimals = _token_deltas(meta, wallet)
        native = _native_delta(tx, wallet)
        quote: dict[str, int] = {
            m: d for m, d in token.items() if m in QUOTE_MINTS and abs(d) >= MIN_QUOTE_RAW[m]
        }
        if abs(native) >= DUST_LAMPORTS:
            quote[NATIVE] = native
        base = {m: d for m, d in token.items() if m not in QUOTE_MINTS}
        if not base:
            return "transfer", []
        if len(base) > 1:
            return "unparsed", []  # token-for-token or multi-leg: not interpreted
        ((mint, delta),) = base.items()
        opposite = {c: d for c, d in quote.items() if (d > 0) != (delta > 0)}
        if not opposite:
            return "transfer", []  # token moved without payment: airdrop/transfer
        counter = max(opposite, key=lambda c: abs(opposite[c]))
        leg = FlowLeg(
            mint=mint,
            side=FlowSide.BUY if delta > 0 else FlowSide.SELL,
            token_delta_raw=delta,
            decimals=decimals[mint],
            counter=counter,
            counter_delta_raw=opposite[counter],
        )
        return "swap", [leg]
    except (KeyError, TypeError, ValueError, IndexError):
        return "unparsed", []
