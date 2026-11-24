"""High-confidence secret scanner for tracked files (no network, never prints a secret value).

Targets: private-key blocks, Solana keypair arrays, well-known provider token formats, API keys
embedded in URLs, and tracked key/env files. Findings carry file, line and rule name only."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

RULES: dict[str, re.Pattern[str]] = {
    "private-key-block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "solana-keypair-array": re.compile(r"\[\s*(?:\d{1,3}\s*,\s*){63}\d{1,3}\s*\]"),
    "aws-access-key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})\b"),
    "slack-token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    "google-api-key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "stripe-live-key": re.compile(r"\bsk_live_[0-9A-Za-z]{16,}\b"),
    "key-in-url": re.compile(
        r"(?i)[?&](?:api[-_]?key|apikey|token|access_token)=[A-Za-z0-9_\-]{20,}"
    ),
    "base58-secret-assignment": re.compile(
        r"(?i)(?:secret|private|keypair|seed|mnemonic)[A-Za-z_]*\s*[=:]\s*['\"]?"
        r"[1-9A-HJ-NP-Za-km-z]{80,90}\b"
    ),
}
FORBIDDEN_FILENAMES = (".env", "id_rsa", "id_ed25519")
FORBIDDEN_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
ALLOW_MARKER = "secretscan: allow"
MAX_BYTES = 1_000_000


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}"


def scan_text(path: str, text: str) -> list[Finding]:
    out: list[Finding] = []
    for no, line in enumerate(text.splitlines(), 1):
        if ALLOW_MARKER in line:
            continue
        for name, pattern in RULES.items():
            if pattern.search(line):
                out.append(Finding(path, no, name))
    return out


def scan_paths(root: Path, paths: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for rel in paths:
        name = Path(rel).name
        if name in FORBIDDEN_FILENAMES or name.endswith(FORBIDDEN_SUFFIXES):
            findings.append(Finding(rel, 0, "forbidden-tracked-file"))
            continue
        f = root / rel
        try:
            if not f.is_file() or f.stat().st_size > MAX_BYTES:
                continue
            findings += scan_text(rel, f.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable: not text secrets
    return findings


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True, text=True
    ).stdout
    return [p for p in out.split("\0") if p]
