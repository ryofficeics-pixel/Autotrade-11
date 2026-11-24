# Operations runbook (PAPER mode)

Live trading is locked; every command below is read-only data tooling or simulation.

## Install and first start
```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -c constraints.txt -e ".[dev]"
cp .env.example .env     # local only; never commit. Set at least AUTOTRADE_USD_IDR_RATE for paper trading
autotrade db init && autotrade doctor --network
autotrade ingest --once  # smoke test
autotrade ingest --watch # continuous (Ctrl+C / SIGTERM = graceful stop after the current cycle)
```
Service (systemd): see `deploy/autotrade.service` and `deploy/autotrade-maintenance.*`. Without root: `deploy/crontab.example`.

## Daily checks
| Command | Meaning |
|---|---|
| `autotrade healthcheck` | exit 0 healthy; otherwise lists issues (kill switch, stale ingestion/market data, provider down, ledger mismatch, DB integrity) |
| `autotrade report daily` | markdown report written to `AUTOTRADE_REPORT_DIR` (default `reports/generated/`) |
| `autotrade status` / `paper status` / `paper report` | counts, freshness, positions, PnL, reconciliation |
| `logs/alerts.log` | alert history (JSON lines); optional webhook via `AUTOTRADE_ALERT_WEBHOOK_URL` |

## Incidents
| Symptom | Action |
|---|---|
| `kill_switch` alert, reason `ledger mismatch` | `autotrade paper reconcile` shows what disagrees. Do NOT release until understood. Restore the last verified backup if the ledger is corrupt. Then `autotrade risk kill-switch off`. |
| kill switch reason `risk engine exception` | Inspect logs (stderr JSON, event `risk engine failure`), fix, then release. |
| `ingest_stale` | Service stopped or hung: `systemctl status autotrade`, journal. Restart is safe (idempotent; no duplicate orders). |
| `provider_*` down/degraded | Public APIs rate-limit or fail. Lower request rates (`AUTOTRADE_*_RPS`), add `SOLANA_RPC_FALLBACK_URLS`, or wait. Entries are blocked automatically while a required provider is unhealthy. |
| `market_data_stale` | No fresh DEX Screener data: entries are blocked (quote age gate). Check provider health first. |
| `db_integrity` | Stop the service; restore from backup (below). |

## Backup and restore
- `autotrade db backup` — consistent online backup (SQLite backup API) verified with `integrity_check`; keeps `AUTOTRADE_BACKUP_KEEP` newest (default 7) in `AUTOTRADE_BACKUP_DIR`. The nightly timer runs it.
- Restore: stop the service; `cp <backup> <db path>; rm -f <db>-wal <db>-shm; autotrade db check; autotrade paper reconcile`; start the service. Data between the backup and the failure is lost; markets keep moving, so gaps are expected.
- Copy backups off the machine; a backup on the same disk does not survive disk failure.

## Kill switch
`autotrade risk kill-switch on --reason "why"` halts new paper entries immediately (persists across restarts). `status` shows it; `off` releases it. Open paper positions still exit by their rules.

## Secrets
Never commit `.env`, keys, RPC URLs with embedded keys or webhook URLs. CI runs a secret scan over tracked files. The application never accepts a private key or seed phrase.

## What is NOT verified
The exit gate "7 days unattended without manual fix" has not been demonstrated: it requires running the service for a week. Treat the first runs as supervised.
