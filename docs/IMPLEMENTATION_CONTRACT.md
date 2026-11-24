# Implementation Contract

## Planned source layout
```text
src/autotrade/
  domain/          # entities, enums, invariants
  config/          # validated safe config
  data/providers/  # isolated RPC/DEX adapters
  data/normalization/
  data/storage/    # SQLite repositories/migrations
  discovery/
  features/
  safety/
  strategies/
  risk/
  backtest/
  paper/
  execution/       # interface only; live locked
  observability/
  cli/
tests/unit/
tests/integration/
tests/replay/
tests/safety/
```

## Stage 1 actual layout (differs from plan above)
`config.py` and `cli.py` remain modules. Added: `domain/` (models, errors, addresses, timeutil), `data/providers/` (base, http, health, solana_rpc, rpc_adapters), `data/storage/` (db, migrations, repositories), `ingestion/` (jobs, engine, scheduler, freshness), `observability/logging.py`, `runtime.py` (composition root). Tests: `tests/{unit,integration,safety}`.

## Dependency direction
Domain has no infrastructure dependency. Providers implement domain-facing interfaces. Features consume normalized records. Strategies emit signals only. Risk evaluates signals and portfolio state. Paper broker consumes approved intents. Live broker absent or hard-disabled. AI helpers cannot mutate risk config or submit orders.

## Coding rules
- Type hints on public interfaces; UTC-aware timestamps.
- Decimal/integer atomic units for accounting.
- Idempotency keys for order intents.
- Explicit errors; no silent broad exception handling.
- Bounded retries/timeouts; structured logs with secret redaction.
- Unit, integration, replay, edge-case, and invariant tests.
- No placeholder that reports success without doing work.
- No synthetic/mock data in production mode.
- Pin dependency versions and document licenses/provider terms.
