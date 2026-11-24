"""Provider-contract implementations backed by Solana RPC only."""

from __future__ import annotations

from pydantic import ValidationError

from autotrade.domain.models import DataQuality, ProviderHealth, Token, WalletActivity
from autotrade.domain.timeutil import from_unix

from .base import FetchResult
from .solana_rpc import SolanaRpcClient

TOKEN_PROGRAMS = frozenset({"spl-token", "spl-token-2022"})


class RpcTokenMetadataProvider:
    """On-chain mint facts only (decimals, authorities, supply). Symbol/name need Metaplex
    metadata, which is out of Stage 1 scope, so they are explicitly None (unknown)."""

    name = "solana_rpc.token"

    def __init__(self, rpc: SolanaRpcClient) -> None:
        self._rpc = rpc

    def health(self) -> ProviderHealth:
        return self._rpc.health()

    async def get_token(self, mint: str) -> FetchResult[Token]:
        resp = await self._rpc.get_account_info(mint)  # ProviderError propagates to the job
        if resp.value is None:
            return FetchResult(rejections=[f"{mint}: mint account not found"], fetched=1)
        try:
            account = resp.value
            data = account["data"]
            parsed, program = data["parsed"], data["program"]
            if program not in TOKEN_PROGRAMS or parsed["type"] != "mint":
                raise ValueError(f"account is not an SPL mint (program={program!r})")
            info = parsed["info"]
            token = Token(
                source=self.name,
                observed_at=resp.received_at,
                received_at=resp.received_at,
                mint=mint,
                decimals=info["decimals"],
                token_program=account.get("owner"),
                authorities_known=True,
                mint_authority=info.get("mintAuthority"),
                freeze_authority=info.get("freezeAuthority"),
                supply_raw=int(info["supply"]),
            )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            return FetchResult(rejections=[f"{mint}: invalid mint account: {exc}"[:300]], fetched=1)
        return FetchResult(items=[token], fetched=1)


class RpcWalletActivityProvider:
    """Recent transaction signatures per wallet (getSignaturesForAddress). Swap/flow parsing
    is not implemented; records are signature-level facts only."""

    name = "solana_rpc.wallet"

    def __init__(self, rpc: SolanaRpcClient) -> None:
        self._rpc = rpc

    def health(self) -> ProviderHealth:
        return self._rpc.health()

    async def get_activity(self, wallet: str, limit: int) -> FetchResult[WalletActivity]:
        resp = await self._rpc.get_signatures_for_address(wallet, limit=limit)
        rows = resp.value
        if not isinstance(rows, list):
            return FetchResult(rejections=[f"{wallet}: unexpected signatures payload"], fetched=0)
        items: list[WalletActivity] = []
        rejections: list[str] = []
        for row in rows:
            try:
                block_time = row.get("blockTime")
                notes: tuple[str, ...] = ()
                quality = DataQuality.OK
                if block_time is None:
                    quality, notes = DataQuality.PARTIAL, ("block_time unavailable",)
                bt = from_unix(block_time) if block_time is not None else None
                items.append(
                    WalletActivity(
                        source=self.name,
                        observed_at=bt or resp.received_at,
                        received_at=resp.received_at,
                        quality=quality,
                        quality_notes=notes,
                        wallet_address=wallet,
                        signature=row["signature"],
                        slot=row["slot"],
                        block_time=bt,
                        succeeded=row.get("err") is None,
                    )
                )
            except (KeyError, TypeError, AttributeError, ValueError, ValidationError) as exc:
                rejections.append(f"{wallet}: invalid signature row: {exc}"[:300])
        return FetchResult(items=items, rejections=rejections, fetched=len(rows))
