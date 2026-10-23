# Execution gateway (executor)

TypeScript service on the official `@meteora-ag/dlmm` SDK. It is the only process that ever
holds wallet material; it builds, simulates, signs and submits DLMM transactions for typed
commands from the Python worker and answers read-only quotes. It listens only inside the
Docker network; every route except `/health/*` requires the `x-internal-token` header.

## Contracts

| Route | Request | Answer |
|---|---|---|
| `POST /v1/simulations` | `DlmmCommandV2` | `SimulationResultV2` |
| `POST /v1/commands` (+ `Idempotency-Key`, `x-mode-token`, approval headers) | `DlmmCommandV2` | `GatewayReceiptV2` |
| `GET /v1/commands/{id}` | — | `GatewayReceiptV2`, reconciled with the chain |
| `POST /v1/quotes` | `QuoteRequestV1` | `QuoteResultV1` |
| `GET /health/live`, `GET /health/ready` | — | process liveness / readiness |

The shared corpora are in `contracts/execution/`: `v2-corpus.json` (execution contract),
`quote-v1-corpus.json` (quotes) and `v1-corpus.json` (the retired execution contract, kept so
both sides prove they reject it). A payload with another `schema_version` is answered with
`400 {"error": "unsupported_contract_version"}`.

What V2 changed against V1, and why it is a new version:

- **The executor creates the position.** An OPEN carries no `position_address`. On simulation
  the executor generates the key of the new position, signs the position initialization with it
  and returns the address in the simulation and in the receipt. The key exists only in memory,
  inside the prepared command, until the command is broadcast, re-simulated, fails or its
  simulation TTL ends; it is never written to the state file, a response or a log.
- **Price impact is measured.** For an entry the swap leg is acquiring `amount_x` of X with Y
  (`swapQuoteExactOut`); for REMOVE/CLOSE it is selling the X the command withdraws from the
  position (`getPosition` + `swapQuote`). The SDK percentage becomes whole bps rounded up. For
  an entry, a quote above `max_slippage_bps` fails the simulation (`SLIPPAGE_BREACH`) before any
  transaction is simulated and an unmeasurable quote (for example insufficient liquidity) fails
  it (`PRICE_IMPACT_UNAVAILABLE`). An exit swaps nothing, so its quote is only reported
  (`price_impact_bps`, `null` when it cannot be quoted) and never stops it: a thin or falling
  pool must not keep a position open. The executor does not swap: the wallet must already hold
  both tokens of an entry, otherwise the transaction simulation fails (`INSUFFICIENT_FUNDS`
  when the program logs say so, `TRANSACTION_SIMULATION_FAILED` otherwise) and nothing is sent.
- **The entry anchor is checked on chain.** OPEN/ADD carry `active_bin_id`,
  `active_bin_tolerance` and the mint decimals the amounts were computed with. The executor
  reads the LB pair's active bin and the mints; a drift beyond the tolerance is
  `ACTIVE_BIN_DRIFT`, other decimals are `TOKEN_DECIMALS_MISMATCH`. The simulation returns the
  observed `active_bin_id`, `token_x_decimals` and `token_y_decimals`.

## Readiness

`/health/ready` is `200 {"status": "ready", "mode": "SHADOW"}` in SHADOW. For APPROVAL and
LIVE it is `200` only when the engine is configured and, at that moment, the wallet file loads
(and matches `WALLET_PUBLIC_KEY` when set), the RPC answers `getLatestBlockhash` within two
seconds and the installed SDK exposes the quote API. Otherwise it is
`503 {"status": "not_ready", "missing": [...]}` with names only: `configuration`, `wallet`,
`rpc`, `price_impact`.

## Configuration

Names only; values are operator-provided (`NAME` or `NAME_FILE` for secrets).

| Variable | Used for |
|---|---|
| `TRADING_MODE` | `SHADOW` (default), `APPROVAL`, `LIVE` |
| `EXECUTOR_INTERNAL_TOKEN(_FILE)` | every `/v1/*` route |
| `EXECUTOR_MODE_TOKEN(_FILE)` | submits |
| `EXECUTOR_APPROVAL_SECRET(_FILE)` | approval proofs (APPROVAL) |
| `SOLANA_RPC_URL` | RPC for quotes (any mode) and execution |
| `WALLET_KEYPAIR_FILE`, `WALLET_PUBLIC_KEY` | wallet (APPROVAL/LIVE) |
| `EXECUTOR_STATE_FILE` | durable submissions and idempotency (APPROVAL/LIVE) |
| `LIVE_PROMOTION_FILE` | operator LIVE promotion evidence (LIVE) |

Quotes need `SOLANA_RPC_URL` and the internal token but never a wallet, so a SHADOW executor
can serve them. Without an RPC, `POST /v1/quotes` answers `503 {"error": "quotes_unavailable"}`.

## Checks

`npm run check` (lint, types, tests). No test opens a network connection or sends a transaction:
the SDK pool, the RPC connection and the wallet are fakes.

## Manual verification on devnet (never automated, never mainnet)

This procedure is the only time the executor touches a real network. It is not part of any test
or default start, and it must be run only against devnet with a throwaway wallet.

1. Create a test wallet and fund it on devnet: `solana-keygen new -o <TEST_WALLET_FILE>` and
   `solana airdrop 2 <TEST_WALLET_PUBKEY> --url devnet`. Never reuse a mainnet key.
2. Pick a devnet DLMM pool `[DEVNET_POOL — впиши]` and hold a small amount of both of its tokens
   in the test wallet (the executor does not swap).
3. Start the executor locally against devnet, outside Compose:
   `npm ci && npm run build`, then with `TRADING_MODE=APPROVAL`,
   `SOLANA_RPC_URL=https://api.devnet.solana.com`, `WALLET_KEYPAIR_FILE=<TEST_WALLET_FILE>`,
   `WALLET_PUBLIC_KEY=<TEST_WALLET_PUBKEY>`, `EXECUTOR_STATE_FILE=<scratch dir>/state.json`,
   `EXECUTOR_INTERNAL_TOKEN`, `EXECUTOR_MODE_TOKEN` and `EXECUTOR_APPROVAL_SECRET` (32+ bytes)
   set to throwaway values, run `npm start`.
4. `GET /health/ready` must be `200 {"status":"ready","mode":"APPROVAL"}`; with the RPC
   unplugged it must be `503` with `"missing":["rpc"]`.
5. `POST /v1/quotes` with `{"schema_version":1,"pool_address":"<DEVNET_POOL>","direction":"Y_TO_X","in_amount":"<small amount>"}`
   must return `success: true`, a `price_impact_bps` and the pool's `active_bin_id` and decimals.
6. Build a tiny OPEN `DlmmCommandV2` (shape: `contracts/execution/v2-corpus.json`,
   `valid_commands[0]`) for the pool: `mode` `APPROVAL`, the wallet, no `position_address`,
   `active_bin_id` from step 5, `active_bin_tolerance` 3, the decimals from step 5, a bin range
   around the active bin, amounts well inside the wallet balance, `expires_at` a few minutes
   ahead. `POST /v1/simulations` must answer `success: true` with a new `position_address`;
   a second simulation must name a different address. Check the executor log and the state file
   contain no key material (no 64-number arrays).
7. Sign the approval for that simulation with the Python helper
   `src.execution.service.sign_operator_approval(secret, approval_id=..., approved_at=...,
   expires_at=..., operator_id=..., command_digest=..., simulation_digest=...)` (approval
   window inside the simulation TTL of two minutes) and `POST /v1/commands` with
   `Idempotency-Key`, `x-mode-token`, `x-approval-id`, `x-approval-approved-at`,
   `x-approval-expires-at`, `x-operator-id`, `x-command-digest`, `x-simulation-digest`,
   `x-approval-proof`. The receipt must carry the simulated `position_address`.
8. Poll `GET /v1/commands/{id}` until `CONFIRMED` and check the position on a devnet explorer:
   it is owned by the test wallet and its address is the one from the receipt.
9. Close it: simulate and submit a `CLOSE` `DlmmCommandV2` on that `position_address`
   (`remove_bps` 10000, no anchor fields) with a fresh approval; the simulation reports the
   exit's `price_impact_bps`.
10. Delete the scratch state file and the test wallet when done.
