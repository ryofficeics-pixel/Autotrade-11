"""CI entry point: fail if a tracked file looks like it contains a secret."""

import sys
from pathlib import Path

from autotrade.ops.secretscan import scan_paths, tracked_files

root = Path(__file__).resolve().parents[1]
findings = scan_paths(root, tracked_files(root))
for f in findings:
    print(f"SECRET? {f}", file=sys.stderr)
print(f"secret scan: {len(findings)} finding(s) in {len(tracked_files(root))} tracked files")
sys.exit(1 if findings else 0)
