import { readFileSync } from "node:fs";

import {
  ComputeBudgetInstruction,
  ComputeBudgetProgram,
  Keypair,
  PublicKey,
  SystemProgram,
  Transaction,
  type Connection,
} from "@solana/web3.js";
import { describe, expect, it } from "vitest";

import {
  parseDlmmCommandV2,
  type DlmmCommandV2,
  type GatewayReceiptV2,
  type SettlementV2,
} from "../src/contracts.js";
import {
  ExecutionEngine,
  GatewayError,
  InMemoryExecutionStateStore,
  type BuiltCommand,
  type ExecutionStateStore,
  type MarketFacts,
  type PersistedSubmission,
  type RpcTransport,
  type SdkBoundary,
  type SettlementBinding,
} from "../src/engine.js";
import {
  OfficialMeteoraDlmmBoundary,
  SolanaRpcTransport,
  computeUnitPrice,
  priorityFeeLamports,
  priorityFeePolicyFromEnvironment,
  walletSettlement,
  type MeteoraDlmmFactory,
  type MeteoraPoolClient,
  type PriorityFeePolicy,
  type SettledTransaction,
} from "../src/sdk.js";
import { buildServer } from "../src/server.js";

const POOL = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo";
const POSITION = "SysvarRent111111111111111111111111111111111";
const MINT_X = "SysvarC1ock11111111111111111111111111111111";
const WSOL = "So11111111111111111111111111111111111111112";
const clock = { now: (): Date => new Date("2026-10-18T12:00:00.000Z") };
const policy: PriorityFeePolicy = {
  percentile: 75,
  minMicroLamports: 1_000n,
  maxLamports: 15_000_000n,
};
const livePromotion = {
  promotionId: "50000000-0000-4000-8000-000000000001",
  rulesetVersion: "guide-v1",
  approvalCycles: 10,
  receiptsReconciled: true,
  riskInvariantsPreserved: true,
  dedicatedRpcReady: true,
  walletReady: true,
  checklistCompleted: true,
  operatorConfirmationId: "operator-confirmation-1",
  promotedAt: "2026-10-18T11:00:00.000Z",
  expiresAt: "2026-10-19T12:00:00.000Z",
};

function swapCommand(
  wallet: string,
  overrides: Partial<Record<string, unknown>> = {},
): DlmmCommandV2 {
  return parseDlmmCommandV2({
    schema_version: 2,
    command_id: "10000000-0000-4000-8000-000000000301",
    idempotency_key: `exit-sale:${POSITION}:2:1`,
    correlation_id: "20000000-0000-4000-8000-000000000301",
    ruleset_version: "guide-v1",
    mode: "LIVE",
    operation: "SWAP",
    pool_address: POOL,
    wallet_address: wallet,
    position_address: POSITION,
    expires_at: "2026-10-18T12:02:00.000Z",
    max_slippage_bps: 100,
    swap_direction: "X_TO_Y",
    in_amount: "125",
    max_price_impact_bps: 300,
    ...overrides,
  });
}

describe("priority fee per compute unit", () => {
  it("falls back to the configured minimum when the network gives no fee data", () => {
    expect(computeUnitPrice([], 200_000, policy)).toBe(1_000n);
  });

  it("takes the configured percentile of recent fees, never below the minimum", () => {
    // Sorted: 1000 2000 3000 4000 5000 7000 8000 9000; nearest rank ceil(0.75 * 8) = 6.
    const fees = [5_000, 1_000, 9_000, 3_000, 7_000, 2_000, 8_000, 4_000];
    expect(computeUnitPrice(fees, 200_000, policy)).toBe(7_000n);
    expect(computeUnitPrice([0, 0, 10, 0], 200_000, policy)).toBe(1_000n);
  });

  it("caps the price so one transaction never pays more than PRIORITY_FEE_MAX_LAMPORTS", () => {
    // floor(15 000 000 lamports * 10^6 / 1 400 000 CU) = 10 714 285 micro-lamports per CU,
    // and ceil(10 714 285 * 1 400 000 / 10^6) = 14 999 999 lamports.
    const price = computeUnitPrice([1_000_000_000_000], 1_400_000, policy);
    expect(price).toBe(10_714_285n);
    expect(priorityFeeLamports(price, 1_400_000)).toBe(14_999_999n);
  });

  it("reads the policy from the environment with 0.015 SOL as the default cap", () => {
    expect(priorityFeePolicyFromEnvironment({})).toEqual({
      percentile: 75,
      minMicroLamports: 1_000n,
      maxLamports: 15_000_000n,
    });
    expect(
      priorityFeePolicyFromEnvironment({
        PRIORITY_FEE_MAX_LAMPORTS: "30000000",
        PRIORITY_FEE_PERCENTILE: "90",
        PRIORITY_FEE_MIN_MICRO_LAMPORTS: "",
      }),
    ).toEqual({ percentile: 90, minMicroLamports: 1_000n, maxLamports: 30_000_000n });
    expect(() => priorityFeePolicyFromEnvironment({ PRIORITY_FEE_PERCENTILE: "101" })).toThrow();
    expect(() => priorityFeePolicyFromEnvironment({ PRIORITY_FEE_MAX_LAMPORTS: "1.5" })).toThrow();
  });
});

function connectionWith(fees: (() => Promise<unknown>) | undefined): {
  connection: Connection;
  feeRequests: unknown[];
} {
  const feeRequests: unknown[] = [];
  const connection = {
    async getLatestBlockhash() {
      return { blockhash: Keypair.generate().publicKey.toBase58(), lastValidBlockHeight: 77 };
    },
    async getFeeForMessage() {
      return { value: 5_000 };
    },
    async simulateTransaction() {
      return { value: { err: null, unitsConsumed: 1_000 } };
    },
    ...(fees === undefined
      ? {}
      : {
          async getRecentPrioritizationFees(config: unknown): Promise<unknown> {
            feeRequests.push(config);
            return fees();
          },
        }),
  } as unknown as Connection;
  return { connection, feeRequests };
}

function builtWith(wallet: Keypair, limit: number | undefined): BuiltCommand {
  const transaction = new Transaction();
  if (limit !== undefined) {
    transaction.add(ComputeBudgetProgram.setComputeUnitLimit({ units: limit }));
  }
  transaction.add(
    SystemProgram.transfer({ fromPubkey: wallet.publicKey, toPubkey: wallet.publicKey, lamports: 1 }),
  );
  return {
    command: swapCommand(wallet.publicKey.toBase58()),
    opaqueTransactions: [transaction],
    opaqueSigners: [wallet],
    positionAddress: POSITION,
    market: { priceImpactBps: 10, activeBinId: 1, tokenXDecimals: 6, tokenYDecimals: 9 },
  };
}

function prices(transaction: Transaction): bigint[] {
  return transaction.instructions
    .filter((instruction) => instruction.programId.equals(ComputeBudgetProgram.programId))
    .filter(
      (instruction) =>
        ComputeBudgetInstruction.decodeInstructionType(instruction) === "SetComputeUnitPrice",
    )
    .map((instruction) =>
      BigInt(ComputeBudgetInstruction.decodeSetComputeUnitPrice(instruction).microLamports),
    );
}

describe("every executor transaction carries a priority fee", () => {
  it("prices compute units by recent network fees of the pool and reports the fee", async () => {
    const wallet = Keypair.generate();
    const { connection, feeRequests } = connectionWith(async () =>
      [1_000, 2_000_000, 3_000, 500].map((prioritizationFee, slot) => ({ slot, prioritizationFee })),
    );
    const built = builtWith(wallet, 300_000);

    const result = await new SolanaRpcTransport(connection, "confirmed", undefined, policy).simulate(
      built,
    );

    // Sorted 500 1000 3000 2000000: nearest rank ceil(0.75 * 4) = 3 -> 3000 micro-lamports.
    const [transaction] = built.opaqueTransactions as Transaction[];
    expect(prices(transaction as Transaction)).toEqual([3_000n]);
    expect(result.priorityFeeLamports).toBe(900n); // ceil(3000 * 300 000 / 10^6)
    expect(result.networkFeeLamports).toBe(5_000n);
    expect(feeRequests).toEqual([{ lockedWritableAccounts: [new PublicKey(POOL)] }]);
  });

  it("uses the minimum price when the RPC cannot give fee data", async () => {
    const wallet = Keypair.generate();
    const sources: Array<(() => Promise<unknown>) | undefined> = [
      undefined,
      async (): Promise<unknown> => [],
      async (): Promise<unknown> => Promise.reject(new Error("down")),
    ];
    for (const fees of sources) {
      const built = builtWith(wallet, undefined);
      const { connection } = connectionWith(fees);

      const result = await new SolanaRpcTransport(connection, "confirmed", undefined, policy).simulate(
        built,
      );

      const [transaction] = built.opaqueTransactions as Transaction[];
      expect(prices(transaction as Transaction)).toEqual([1_000n]);
      // Without a limit instruction the runtime grants 200 000 CU per instruction.
      expect(result.priorityFeeLamports).toBe(200n);
    }
  });

  it("never lets the price exceed the cap whatever the network asks", async () => {
    const wallet = Keypair.generate();
    const { connection } = connectionWith(async () => [{ slot: 1, prioritizationFee: 10 ** 15 }]);
    const built = builtWith(wallet, 1_400_000);

    const result = await new SolanaRpcTransport(connection, "confirmed", undefined, policy).simulate(
      built,
    );

    expect(result.priorityFeeLamports).toBe(14_999_999n);
    expect(result.priorityFeeLamports <= policy.maxLamports).toBe(true);
  });
});

class SwapPool implements MeteoraPoolClient {
  readonly swaps: Array<Record<string, string | string[]>> = [];
  readonly quotes: Array<{ amount: string; swapForY: boolean; slippage: string }> = [];
  pubkey = new PublicKey(POOL);
  lbPair = { activeId: 8001 };
  tokenX = { publicKey: new PublicKey(MINT_X), mint: { decimals: 6 } };
  tokenY = { publicKey: new PublicKey(WSOL), mint: { decimals: 9 } };
  binArrays = [Keypair.generate().publicKey, Keypair.generate().publicKey];
  quoteFailure: Error | undefined;

  constructor(
    private readonly wallet: Keypair,
    private readonly quote: { consumed: string; out: string; fee: string; impact: string },
  ) {}

  async getBinArrayForSwap(swapForY: boolean): Promise<unknown[]> {
    return [{ swapForY }];
  }

  swapQuote(
    inAmount: { toString(): string },
    swapForY: boolean,
    allowedSlippage: { toString(): string },
  ): {
    consumedInAmount: { toString(): string };
    outAmount: { toString(): string };
    fee: { toString(): string };
    minOutAmount: { toString(): string };
    priceImpact: { toFixed(): string };
    binArraysPubkey: PublicKey[];
  } {
    if (this.quoteFailure !== undefined) {
      throw this.quoteFailure;
    }
    this.quotes.push({
      amount: inAmount.toString(),
      swapForY,
      slippage: allowedSlippage.toString(),
    });
    return {
      consumedInAmount: { toString: () => this.quote.consumed },
      outAmount: { toString: () => this.quote.out },
      fee: { toString: () => this.quote.fee },
      // Deliberately not the executor's own minimum: the transaction uses its own rule.
      minOutAmount: { toString: () => "1" },
      priceImpact: { toFixed: () => this.quote.impact },
      binArraysPubkey: this.binArrays,
    };
  }

  swapQuoteExactOut(): { priceImpact: { toFixed(): string } } {
    throw new Error("a sale is exact in");
  }

  async getPosition(): Promise<{ positionData: { totalXAmount: string } }> {
    throw new Error("a swap never reads a position");
  }

  async swap(parameters: {
    inToken: PublicKey;
    outToken: PublicKey;
    inAmount: { toString(): string };
    minOutAmount: { toString(): string };
    lbPair: PublicKey;
    user: PublicKey;
    binArraysPubkey: PublicKey[];
  }): Promise<Transaction> {
    this.swaps.push({
      inToken: parameters.inToken.toBase58(),
      outToken: parameters.outToken.toBase58(),
      inAmount: parameters.inAmount.toString(),
      minOutAmount: parameters.minOutAmount.toString(),
      lbPair: parameters.lbPair.toBase58(),
      user: parameters.user.toBase58(),
      binArraysPubkey: parameters.binArraysPubkey.map((key) => key.toBase58()),
    });
    return new Transaction().add(
      SystemProgram.transfer({
        fromPubkey: this.wallet.publicKey,
        toPubkey: this.wallet.publicKey,
        lamports: 1,
      }),
    );
  }

  async initializePositionAndAddLiquidityByStrategy(): Promise<Transaction> {
    throw new Error("unexpected");
  }

  async addLiquidityByStrategy(): Promise<Transaction> {
    throw new Error("unexpected");
  }

  async removeLiquidity(): Promise<Transaction[]> {
    throw new Error("unexpected");
  }
}

function factoryFor(pool: MeteoraPoolClient): MeteoraDlmmFactory {
  return {
    StrategyType: { Spot: 0, BidAsk: 2 },
    async create(_connection: Connection, address: PublicKey): Promise<MeteoraPoolClient> {
      expect(address.toBase58()).toBe(POOL);
      return pool;
    },
  };
}

describe("official SDK boundary: the sale of the base token", () => {
  it("builds an exact-in X->Y swap in the same pool with the quote's minimum output", async () => {
    const wallet = Keypair.generate();
    const pool = new SwapPool(wallet, { consumed: "125", out: "2500", fee: "25", impact: "1.8" });
    const boundary = new OfficialMeteoraDlmmBoundary(
      {} as Connection,
      { loadWalletSigner: async (): Promise<Keypair> => wallet },
      factoryFor(pool),
    );

    const built = await boundary.build(swapCommand(wallet.publicKey.toBase58()));

    expect(pool.quotes).toEqual([{ amount: "125", swapForY: true, slippage: "100" }]);
    expect(pool.swaps).toEqual([
      {
        inToken: MINT_X,
        outToken: WSOL,
        inAmount: "125",
        minOutAmount: "2475", // floor(2500 * (10 000 - 100) / 10 000)
        lbPair: POOL,
        user: wallet.publicKey.toBase58(),
        binArraysPubkey: pool.binArrays.map((key) => key.toBase58()),
      },
    ]);
    expect(built.positionAddress).toBe(POSITION);
    expect(built.market).toMatchObject({
      priceImpactBps: 180,
      activeBinId: 8001,
      tokenXMint: MINT_X,
      tokenYMint: WSOL,
      swap: { inAmount: "125", outAmount: "2500", minOutAmount: "2475", feeAmount: "25" },
    });
  });

  it("names a quote refused for liquidity by the SDK's own code, so the caller splits it", async () => {
    // The official SDK throws DlmmSdkError(name "SWAP_QUOTE_INSUFFICIENT_LIQUIDITY") when the
    // bin arrays cannot fill the amount; any other failure leaves the impact unknown.
    const wallet = Keypair.generate();
    const pool = new SwapPool(wallet, { consumed: "125", out: "2500", fee: "25", impact: "1.8" });
    const boundary = new OfficialMeteoraDlmmBoundary(
      {} as Connection,
      { loadWalletSigner: async (): Promise<Keypair> => wallet },
      factoryFor(pool),
    );
    const thin = new Error("Insufficient liquidity in binArrays");
    thin.name = "SWAP_QUOTE_INSUFFICIENT_LIQUIDITY";

    pool.quoteFailure = thin;
    const engine = engineFor(boundary, new FakeRpc());
    const refused = await engine.simulate(swapCommand(wallet.publicKey.toBase58()));

    expect(refused.success).toBe(false);
    expect(refused.errors.map((error) => error.code)).toEqual(["SWAP_QUOTE_INSUFFICIENT_LIQUIDITY"]);
    pool.quoteFailure = new Error("bin array fetch failed");
    await expect(boundary.build(swapCommand(wallet.publicKey.toBase58()))).rejects.toMatchObject({
      code: "PRICE_IMPACT_UNAVAILABLE",
    });
    expect(pool.swaps).toEqual([]);
  });
});

const swapFacts = (impact: number, consumed = "125"): MarketFacts => ({
  priceImpactBps: impact,
  activeBinId: 8001,
  tokenXDecimals: 6,
  tokenYDecimals: 9,
  tokenXMint: MINT_X,
  tokenYMint: WSOL,
  swap: { inAmount: consumed, outAmount: "2500", minOutAmount: "2475", feeAmount: "25" },
});

class FakeSdk implements SdkBoundary {
  constructor(public market: MarketFacts) {}

  async build(command: DlmmCommandV2): Promise<BuiltCommand> {
    return {
      command,
      opaqueTransactions: [new Uint8Array([1])],
      positionAddress: command.position_address ?? POSITION,
      market: this.market,
    };
  }
}

class FakeRpc implements RpcTransport {
  submitCalls = 0;
  statusState: "PENDING" | "CONFIRMED" = "CONFIRMED";
  settlementCalls: Array<{ signatures: readonly string[]; binding: SettlementBinding }> = [];
  settlementAnswer: SettlementV2 | undefined = undefined;

  async simulate(): Promise<{
    computeUnits: number;
    networkFeeLamports: bigint;
    priorityFeeLamports: bigint;
    blockhash: string;
    lastValidBlockHeight: number;
    logs: string[];
  }> {
    return {
      computeUnits: 60_000,
      networkFeeLamports: 5_000n,
      priorityFeeLamports: 1_200n,
      blockhash: "safe-blockhash",
      lastValidBlockHeight: 9,
      logs: [],
    };
  }

  async submit(): Promise<string> {
    this.submitCalls += 1;
    return "swap-signature-1";
  }

  async status(): Promise<{ state: "PENDING" | "CONFIRMED"; slot: number }> {
    return { state: this.statusState, slot: 7 };
  }

  async settlement(
    signatures: readonly string[],
    binding: SettlementBinding,
  ): Promise<SettlementV2 | undefined> {
    this.settlementCalls.push({ signatures, binding });
    return this.settlementAnswer;
  }
}

function engineFor(
  sdk: SdkBoundary,
  rpc: RpcTransport,
  mode: "LIVE" | "APPROVAL" = "LIVE",
  store?: ExecutionStateStore,
): ExecutionEngine {
  return new ExecutionEngine({
    sdk,
    rpc,
    clock,
    submissionEnabled: true,
    allowedMode: mode,
    livePromotion,
    ...(store === undefined ? {} : { store }),
  });
}

const WALLET = "11111111111111111111111111111111";

let recordedCount = 0;

/** A submission the executor recorded earlier, as its durable store holds it. */
function recorded(
  operation: "CLOSE" | "REMOVE" | "SWAP",
  key: string,
  state: "CONFIRMED" | "SUBMITTED" | "FAILED",
  options: { deltaX?: string; inAmount?: string; position?: string } = {},
): PersistedSubmission {
  recordedCount += 1;
  const commandId = `10000000-0000-4000-8000-${(900_000 + recordedCount).toString().padStart(12, "0")}`;
  return {
    commandId,
    commandDigest: "a".repeat(64),
    simulationDigest: "b".repeat(64),
    idempotencyKey: key,
    operation,
    ...(options.inAmount === undefined ? {} : { inAmount: options.inAmount }),
    settlementBinding: { wallet: WALLET, tokenXMint: MINT_X, tokenYMint: WSOL },
    receipt: {
      schema_version: 2,
      command_id: commandId,
      command_digest: "a".repeat(64),
      simulation_digest: "b".repeat(64),
      idempotency_key: key,
      state,
      position_address: options.position ?? POSITION,
      signature: state === "FAILED" ? null : `${operation.toLowerCase()}-signature`,
      signatures: state === "FAILED" ? [] : [`${operation.toLowerCase()}-signature`],
      transaction_count: 1,
      submitted_at: "2026-10-18T11:59:00.000Z",
      attempt_count: 1,
      error: null,
      settlement:
        options.deltaX === undefined
          ? null
          : {
              fee_lamports: "5000",
              priority_fee_lamports: "0",
              delta_x: options.deltaX,
              delta_y: "0",
              mint_x: MINT_X,
              mint_y: WSOL,
            },
    },
  };
}

/** The executor's store after the position's exit returned `returned` base units. */
async function storeAfterExit(
  returned: string,
  ...more: PersistedSubmission[]
): Promise<InMemoryExecutionStateStore> {
  const store = new InMemoryExecutionStateStore();
  for (const item of [
    recorded("CLOSE", `position-action:${POSITION}:2`, "CONFIRMED", { deltaX: returned }),
    ...more,
  ]) {
    await store.reserve(item);
  }
  return store;
}

/** The SDK quoting exactly the part it is asked for. */
class SaleSdk implements SdkBoundary {
  async build(command: DlmmCommandV2): Promise<BuiltCommand> {
    return {
      command,
      opaqueTransactions: [new Uint8Array([1])],
      positionAddress: command.position_address ?? POSITION,
      market: swapFacts(180, command.in_amount ?? "0"),
    };
  }
}

function part(number: number, inAmount: string, position = POSITION): DlmmCommandV2 {
  return swapCommand(WALLET, {
    command_id: `10000000-0000-4000-8000-0000000004${number.toString().padStart(2, "0")}`,
    idempotency_key: `exit-sale:${position}:2:${number}`,
    position_address: position,
    in_amount: inAmount,
  });
}

async function sell(engine: ExecutionEngine, command: DlmmCommandV2): Promise<GatewayReceiptV2> {
  const simulation = await engine.simulate(command);
  expect(simulation.success).toBe(true);
  return engine.submit(command, command.idempotency_key, { modeTokenVerified: true });
}

describe("execution engine: SWAP", () => {
  it("refuses a swap whose quoted impact exceeds its own limit and reports the quote", async () => {
    const engine = engineFor(new FakeSdk(swapFacts(640)), new FakeRpc());

    const simulation = await engine.simulate(swapCommand(WALLET));

    expect(simulation.success).toBe(false);
    expect(simulation.errors.map((error) => error.code)).toEqual(["PRICE_IMPACT_BREACH"]);
    expect(simulation.price_impact_bps).toBe(640);
    expect(simulation.swap_quote?.out_amount).toBe("2500");
  });

  it("refuses a quote that would not sell the whole amount", async () => {
    const engine = engineFor(new FakeSdk(swapFacts(10, "100")), new FakeRpc());

    const simulation = await engine.simulate(swapCommand(WALLET));

    expect(simulation.errors.map((error) => error.code)).toEqual(["SWAP_QUOTE_INCOMPLETE"]);
  });

  it("simulates a swap within its limit without a bin range and submits it once per key", async () => {
    const rpc = new FakeRpc();
    const engine = engineFor(new FakeSdk(swapFacts(180)), rpc, "LIVE", await storeAfterExit("125"));
    const command = swapCommand(WALLET);

    const simulation = await engine.simulate(command);
    const first = await engine.submit(command, command.idempotency_key, { modeTokenVerified: true });
    const again = await engine.submit(command, command.idempotency_key, { modeTokenVerified: true });

    expect(simulation).toMatchObject({
      success: true,
      min_bin_id: null,
      max_bin_id: null,
      position_address: POSITION,
      price_impact_bps: 180,
      priority_fee_lamports: "1200",
      swap_quote: { in_amount: "125", out_amount: "2500", min_out_amount: "2475", fee_amount: "25" },
    });
    expect(first.state).toBe("SUBMITTED");
    expect(again.signatures).toEqual(first.signatures);
    expect(rpc.submitCalls).toBe(1);
  });

  it("sends an APPROVAL swap only with the operator's approval of that exact payload", async () => {
    const rpc = new FakeRpc();
    const engine = engineFor(new FakeSdk(swapFacts(180)), rpc, "APPROVAL");
    const command = swapCommand(WALLET, { mode: "APPROVAL" });

    await engine.simulate(command);

    await expect(
      engine.submit(command, command.idempotency_key, { modeTokenVerified: true }),
    ).rejects.toMatchObject({ code: "VALID_APPROVAL_REQUIRED" } satisfies Partial<GatewayError>);
    expect(rpc.submitCalls).toBe(0);
  });

  it("attaches the chain settlement once the command is confirmed", async () => {
    const rpc = new FakeRpc();
    const engine = engineFor(new FakeSdk(swapFacts(180)), rpc, "LIVE", await storeAfterExit("125"));
    const command = swapCommand(WALLET);
    await engine.simulate(command);
    await engine.submit(command, command.idempotency_key, { modeTokenVerified: true });

    const unread = await engine.get(command.command_id);
    rpc.settlementAnswer = {
      fee_lamports: "6200",
      priority_fee_lamports: "1200",
      delta_x: "-125",
      delta_y: "2490",
      mint_x: MINT_X,
      mint_y: WSOL,
    };
    const settled = await engine.get(command.command_id);
    const reread = await engine.get(command.command_id);

    expect(unread?.state).toBe("CONFIRMED");
    expect(unread?.settlement).toBeUndefined();
    expect(settled?.settlement).toEqual(rpc.settlementAnswer);
    expect(reread?.settlement).toEqual(rpc.settlementAnswer);
    expect(rpc.settlementCalls).toHaveLength(2);
    expect(rpc.settlementCalls[0]).toEqual({
      signatures: ["swap-signature-1"],
      binding: { wallet: WALLET, tokenXMint: MINT_X, tokenYMint: WSOL },
    });
  });

  it("serves the swap through the internal routes with an idempotency key", async () => {
    const rpc = new FakeRpc();
    const engine = engineFor(new FakeSdk(swapFacts(180)), rpc, "LIVE", await storeAfterExit("125"));
    const server = buildServer(
      {},
      { engine, internalToken: "internal", modeToken: "mode", configuredMode: "LIVE" },
    );
    const payload = swapCommand(WALLET);
    const headers = { "x-internal-token": "internal" };
    const submitHeaders = {
      ...headers,
      "x-mode-token": "mode",
      "idempotency-key": payload.idempotency_key,
    };

    const simulation = await server.inject({ method: "POST", url: "/v1/simulations", headers, payload });
    const first = await server.inject({ method: "POST", url: "/v1/commands", headers: submitHeaders, payload });
    const again = await server.inject({ method: "POST", url: "/v1/commands", headers: submitHeaders, payload });
    await server.close();

    expect(simulation.json()).toMatchObject({ success: true, swap_quote: { min_out_amount: "2475" } });
    expect(first.json()).toMatchObject({ state: "SUBMITTED", position_address: POSITION });
    expect(again.json()).toMatchObject({ signatures: first.json().signatures });
    expect(rpc.submitCalls).toBe(1);
  });
});

describe("execution engine: a sale never exceeds what the position's exits returned", () => {
  it("submits parts up to the confirmed exits' delta_x and refuses the part beyond it", async () => {
    // The CLOSE returned 200: 125 + 100 would be 225, so that part is refused unsent;
    // 125 + 75 is exactly 200.
    const rpc = new FakeRpc();
    const engine = engineFor(new SaleSdk(), rpc, "LIVE", await storeAfterExit("200"));

    await sell(engine, part(1, "125"));
    await expect(sell(engine, part(2, "100"))).rejects.toMatchObject({
      code: "SWAP_EXCEEDS_EXIT_RETURNS",
    } satisfies Partial<GatewayError>);
    const submitsAfterRefusal = rpc.submitCalls;
    const last = await sell(engine, part(3, "75"));

    expect(submitsAfterRefusal).toBe(1);
    expect(last.state).toBe("SUBMITTED");
    expect(rpc.submitCalls).toBe(2);
    expect(await engine.lookup(part(2, "100").command_id)).toBeUndefined();
  });

  it("counts sold parts by their settlement and never counts a failed one", async () => {
    // Exits returned 200 + 50; a part of 150 failed on chain, one of 60 sold 60 by its
    // settlement: 250 - 60 = 190 may still be sold, 191 may not.
    const store = await storeAfterExit(
      "200",
      recorded("REMOVE", `position-action:${POSITION}:1`, "CONFIRMED", { deltaX: "50" }),
      recorded("SWAP", `exit-sale:${POSITION}:1:1`, "FAILED", { inAmount: "150" }),
      recorded("SWAP", `exit-sale:${POSITION}:1:2`, "CONFIRMED", {
        inAmount: "60",
        deltaX: "-60",
      }),
    );
    const rpc = new FakeRpc();
    const engine = engineFor(new SaleSdk(), rpc, "LIVE", store);

    await expect(sell(engine, part(1, "191"))).rejects.toMatchObject({
      code: "SWAP_EXCEEDS_EXIT_RETURNS",
    });
    const receipt = await sell(engine, part(2, "190"));

    expect(receipt.state).toBe("SUBMITTED");
    expect(rpc.submitCalls).toBe(1);
  });

  it("refuses a sale of a position whose exit is not confirmed or belongs to another position", async () => {
    const other = "SysvarStakeHistory1111111111111111111111111";
    const store = new InMemoryExecutionStateStore();
    await store.reserve(recorded("CLOSE", `position-action:${POSITION}:1`, "SUBMITTED"));
    await store.reserve(
      recorded("CLOSE", `position-action:${other}:1`, "CONFIRMED", { deltaX: "500", position: other }),
    );
    const rpc = new FakeRpc();
    const engine = engineFor(new SaleSdk(), rpc, "LIVE", store);

    await expect(sell(engine, part(1, "1"))).rejects.toMatchObject({
      code: "SWAP_EXCEEDS_EXIT_RETURNS",
    });
    expect(rpc.submitCalls).toBe(0);
  });

  it("refuses over the internal route with the shared corpus answer", async () => {
    const v2 = JSON.parse(
      readFileSync(new URL("../../contracts/execution/v2-corpus.json", import.meta.url), "utf8"),
    ) as { submit_refusals: Array<{ status: number; body: unknown; outcome: string }> };
    const refusal = v2.submit_refusals.find((item) => item.outcome === "REJECTED");
    const engine = engineFor(new SaleSdk(), new FakeRpc(), "LIVE", await storeAfterExit("100"));
    const server = buildServer(
      {},
      { engine, internalToken: "internal", modeToken: "mode", configuredMode: "LIVE" },
    );
    const payload = part(1, "125");
    const headers = { "x-internal-token": "internal" };

    await server.inject({ method: "POST", url: "/v1/simulations", headers, payload });
    const response = await server.inject({
      method: "POST",
      url: "/v1/commands",
      headers: { ...headers, "x-mode-token": "mode", "idempotency-key": payload.idempotency_key },
      payload,
    });
    await server.close();

    expect(refusal).toBeDefined();
    expect(response.statusCode).toBe(refusal?.status);
    expect(response.json()).toEqual(refusal?.body);
  });
});

describe("wallet settlement from the chain", () => {
  const wallet = "Wa11et1111111111111111111111111111111111111";

  it("counts token deltas and unwrapped SOL net of the fee and token-account rent", () => {
    // Swap of 125 X for SOL: the X account is kept; a pre-existing WSOL account holding 40
    // (rent 2039280) receives 2490 and is closed into the wallet. The wallet paid 6200.
    const settlement = walletSettlement(
      [
        {
          fee: 6_200n,
          signatureCount: 1,
          walletIndex: 0,
          preBalances: [1_000_000_000, 2_039_320, 2_039_280],
          postBalances: [1_000_000_000 - 6_200 + 2_039_280 + 40 + 2_490, 0, 2_039_280],
          preTokenBalances: [
            { accountIndex: 1, mint: WSOL, owner: wallet, uiTokenAmount: { amount: "40" } },
            { accountIndex: 2, mint: MINT_X, owner: wallet, uiTokenAmount: { amount: "500" } },
          ],
          postTokenBalances: [
            { accountIndex: 2, mint: MINT_X, owner: wallet, uiTokenAmount: { amount: "375" } },
          ],
        },
      ],
      { wallet, tokenXMint: MINT_X, tokenYMint: WSOL },
    );

    expect(settlement).toEqual({
      fee_lamports: "6200",
      priority_fee_lamports: "1200",
      delta_x: "-125",
      delta_y: "2490",
      mint_x: MINT_X,
      mint_y: WSOL,
    });
  });

  it("sums several transactions and ignores accounts of other owners", () => {
    const tx = (x: string, fee: bigint): SettledTransaction => ({
      fee,
      signatureCount: 2,
      walletIndex: 0,
      preBalances: [10_000_000, 0, 0],
      postBalances: [10_000_000 - Number(fee), 0, 0],
      preTokenBalances: [
        { accountIndex: 1, mint: MINT_X, owner: wallet, uiTokenAmount: { amount: "0" } },
        { accountIndex: 2, mint: MINT_X, owner: POSITION, uiTokenAmount: { amount: "999" } },
      ],
      postTokenBalances: [
        { accountIndex: 1, mint: MINT_X, owner: wallet, uiTokenAmount: { amount: x } },
        { accountIndex: 2, mint: MINT_X, owner: POSITION, uiTokenAmount: { amount: "0" } },
      ],
    });

    const settlement = walletSettlement([tx("70", 10_000n), tx("55", 11_000n)], {
      wallet,
      tokenXMint: MINT_X,
      tokenYMint: WSOL,
    });

    // 70 + 55 of X; fees 21 000 of which 21 000 - 4 * 5 000 above the signature fees.
    expect(settlement).toEqual({
      fee_lamports: "21000",
      priority_fee_lamports: "1000",
      delta_x: "125",
      delta_y: "0",
      mint_x: MINT_X,
      mint_y: WSOL,
    });
  });
});
