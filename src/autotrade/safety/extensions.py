"""Token-2022 extension risk analysis for traders (what can hurt a buyer who wants to sell).

Benign: metadata/grouping/ownership-hygiene extensions. Dangerous: a delegate that can move or burn
holder tokens, a transfer hook (arbitrary code on every transfer), non-transferable mints, frozen
default accounts, confidential transfers (amounts cannot be audited), pausable mints with a live
authority, and transfer fees that are high or changeable. Anything not recognised is UNKNOWN."""

from __future__ import annotations

import json

from autotrade.config import SafetyPolicy
from autotrade.domain.models import RuleResult, RuleStatus

BENIGN = frozenset(
    {
        "metadataPointer",
        "tokenMetadata",
        "groupPointer",
        "groupMemberPointer",
        "tokenGroup",
        "tokenGroupMember",
        "immutableOwner",
        "memoTransfer",
        "cpiGuard",
        "mintCloseAuthority",
    }
)
ALWAYS_DANGEROUS = {
    "nonTransferable": "mint is non-transferable",
    "confidentialTransferMint": "confidential transfers hide amounts",
    "confidentialTransferFeeConfig": "confidential transfer fees",
    "confidentialMintBurn": "confidential mint/burn",
}


def analyze(raw_json: str | None, policy: SafetyPolicy) -> RuleResult:
    rule = "token_extensions"
    if raw_json is None:
        return RuleResult(
            rule=rule,
            status=RuleStatus.UNKNOWN,
            detail="extensions were not analysed for this mint",
        )
    try:
        items = json.loads(raw_json)
    except ValueError:
        return RuleResult(rule=rule, status=RuleStatus.UNKNOWN, detail="unreadable extension data")
    if not items:
        return RuleResult(rule=rule, status=RuleStatus.PASS, observed="none")
    problems: list[str] = []
    unknown: list[str] = []
    for item in items:
        name, d = item["name"], item.get("detail") or {}
        if name in BENIGN:
            continue
        if name in ALWAYS_DANGEROUS:
            problems.append(f"{name}: {ALWAYS_DANGEROUS[name]}")
        elif name == "permanentDelegate":
            if d.get("delegate"):
                problems.append("permanentDelegate: a delegate can move/burn any holder's tokens")
        elif name == "transferHook":
            if d.get("program_id"):
                problems.append("transferHook: custom program runs on every transfer")
        elif name in ("pausableConfig", "pausable"):
            if d.get("authority"):
                problems.append("pausable: an authority can pause all transfers")
        elif name == "defaultAccountState":
            if d.get("state") not in (None, "initialized"):
                problems.append(f"defaultAccountState: new accounts start {d.get('state')}")
        elif name == "transferFeeConfig":
            bps = d.get("fee_bps")
            if bps is None:
                unknown.append("transferFeeConfig: fee unreadable")
            elif int(bps) > policy.max_transfer_fee_bps:
                problems.append(f"transferFeeConfig: fee {bps} bps > {policy.max_transfer_fee_bps}")
            elif d.get("config_authority"):
                problems.append("transferFeeConfig: authority can change the fee later")
        else:
            unknown.append(f"{name}: not recognised")
    names = ",".join(i["name"] for i in items)
    if problems:
        return RuleResult(
            rule=rule, status=RuleStatus.FAIL, observed=names, detail="; ".join(problems)
        )
    if unknown:
        return RuleResult(
            rule=rule, status=RuleStatus.UNKNOWN, observed=names, detail="; ".join(unknown)
        )
    return RuleResult(rule=rule, status=RuleStatus.PASS, observed=names)
