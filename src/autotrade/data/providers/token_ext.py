"""Token-2022 extension normalisation from `getAccountInfo` (jsonParsed) mint data.

Live shape (verified on mainnet): `data.parsed.info.extensions` is a list of
`{"extension": <name>, "state": {...}}`. Only the fields the safety screen needs are kept."""

from __future__ import annotations

from typing import Any

from autotrade.domain.models import TokenExtension


def _s(v: Any) -> str | None:
    return None if v is None else str(v)


def _fee_bps(state: dict[str, Any]) -> str | None:
    # Both the current and the scheduled ("older") fee matter; report the larger one.
    values = []
    for key in ("newerTransferFee", "olderTransferFee"):
        fee = state.get(key)
        if isinstance(fee, dict) and fee.get("transferFeeBasisPoints") is not None:
            values.append(int(fee["transferFeeBasisPoints"]))
    return str(max(values)) if values else None


def normalize_extensions(raw: Any) -> tuple[TokenExtension, ...]:
    """Raises ValueError on malformed input (the caller rejects the token record)."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("extensions must be a list")
    out: list[TokenExtension] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("extension"), str):
            raise ValueError("malformed extension entry")
        name, state = item["extension"], item.get("state") or {}
        if not isinstance(state, dict):
            raise ValueError(f"malformed state for {name}")
        detail: dict[str, str | None] = {}
        if name == "transferFeeConfig":
            detail = {
                "fee_bps": _fee_bps(state),
                "config_authority": _s(state.get("transferFeeConfigAuthority")),
            }
        elif name == "permanentDelegate":
            detail = {"delegate": _s(state.get("delegate"))}
        elif name == "transferHook":
            detail = {
                "program_id": _s(state.get("programId")),
                "authority": _s(state.get("authority")),
            }
        elif name in ("pausableConfig", "pausable"):
            detail = {"authority": _s(state.get("authority"))}
        elif name == "defaultAccountState":
            detail = {"state": _s(state.get("accountState"))}
        elif name == "mintCloseAuthority":
            detail = {"authority": _s(state.get("closeAuthority"))}
        out.append(TokenExtension(name=name, detail=detail))
    return tuple(out)


def metadata_names(raw: Any) -> tuple[str | None, str | None]:
    """On-chain (verified) symbol/name from the tokenMetadata extension, if present."""
    for item in raw or []:
        if isinstance(item, dict) and item.get("extension") == "tokenMetadata":
            st = item.get("state") or {}
            sym, nm = st.get("symbol"), st.get("name")
            return (
                sym.strip() or None if isinstance(sym, str) else None,
                nm.strip() or None if isinstance(nm, str) else None,
            )
    return None, None
