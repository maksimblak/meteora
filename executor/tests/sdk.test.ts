import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Writable } from "node:stream";

import { Keypair, PublicKey, SystemProgram, Transaction, type Connection } from "@solana/web3.js";
import { afterEach, describe, expect, it } from "vitest";

import { parseDlmmCommandV2, type DlmmCommandV2 } from "../src/contracts.js";
import {
  ExecutionEngine,
  FileExecutionStateStore,
  type LivePromotionEvidence,
  type SdkBoundary,
} from "../src/engine.js";
import {
  FileSecretSignerProvider,
  OfficialMeteoraDlmmBoundary,
  SolanaRpcTransport,
  priceImpactBps,
  type MeteoraDlmmFactory,
  type MeteoraPoolClient,
  type SecretSignerProvider,
} from "../src/sdk.js";
import { buildServer } from "../src/server.js";

const temporaryDirectories: string[] = [];
const EXISTING_POSITION = "SysvarRent111111111111111111111111111111111";
const POOL = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo";
const clock = { now: (): Date => new Date("2026-10-18T12:00:00.000Z") };
const livePromotion: LivePromotionEvidence = {
  promotionId: "50000000-0000-4000-8000-000000000001",
  rulesetVersion: "rules-2026-10-18",
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

afterEach(async () => {
  await Promise.all(temporaryDirectories.splice(0).map((path) => rm(path, { recursive: true })));
});

function command(
  wallet: Keypair,
  operation: "OPEN" | "ADD" | "REMOVE" | "CLOSE",
  amountX = "1000",
): DlmmCommandV2 {
  const addsLiquidity = operation === "OPEN" || operation === "ADD";
  return parseDlmmCommandV2({
    schema_version: 2,
    command_id: `10000000-0000-4000-8000-00000000000${operation.length}`,
    idempotency_key: `operation-${operation.toLowerCase()}`,
    correlation_id: "20000000-0000-4000-8000-000000000001",
    ruleset_version: "rules-2026-10-18",
    mode: "LIVE",
    operation,
    pool_address: POOL,
    wallet_address: wallet.publicKey.toBase58(),
    ...(operation === "OPEN" ? {} : { position_address: EXISTING_POSITION }),
    expires_at: "2030-01-02T03:04:05Z",
    max_slippage_bps: 50,
    min_bin_id: 7996,
    max_bin_id: 8007,
    ...(addsLiquidity
      ? {
          strategy: operation === "ADD" ? "BID_ASK" : "SPOT",
          amount_x: amountX,
          amount_y: "2000",
          active_bin_id: 8000,
          active_bin_tolerance: 2,
          token_x_decimals: 6,
          token_y_decimals: 9,
        }
      : { remove_bps: operation === "CLOSE" ? 10_000 : 5_000 }),
  });
}

interface Call {
  operation: string;
  parameters: Record<string, unknown>;
}

class FakePool implements MeteoraPoolClient {
  readonly calls: Call[] = [];
  readonly quotes: Array<{ kind: string; amount: string; swapForY: boolean; slippage: string }> = [];
  pubkey = new PublicKey(POOL);
  lbPair = { activeId: 8001 };
  tokenX = { publicKey: new PublicKey("SysvarC1ock11111111111111111111111111111111"), mint: { decimals: 6 } };
  tokenY = { publicKey: new PublicKey("So11111111111111111111111111111111111111112"), mint: { decimals: 9 } };
  quoteFailure: Error | undefined;

  constructor(private readonly wallet: Keypair) {}

  async getBinArrayForSwap(swapForY: boolean): Promise<unknown[]> {
    return [{ swapForY }];
  }

  swapQuote(
    inAmount: { toString(): string },
    swapForY: boolean,
    allowedSlippage: { toString(): string },
  ): { priceImpact: { toFixed(): string } } {
    if (this.quoteFailure !== undefined) {
      throw this.quoteFailure;
    }
    this.quotes.push({
      kind: "exact-in",
      amount: inAmount.toString(),
      swapForY,
      slippage: allowedSlippage.toString(),
    });
    return { priceImpact: { toFixed: () => "0.5" } };
  }

  swapQuoteExactOut(
    outAmount: { toString(): string },
    swapForY: boolean,
    allowedSlippage: { toString(): string },
  ): { priceImpact: { toFixed(): string } } {
    if (this.quoteFailure !== undefined) {
      throw this.quoteFailure;
    }
    this.quotes.push({
      kind: "exact-out",
      amount: outAmount.toString(),
      swapForY,
      slippage: allowedSlippage.toString(),
    });
    return { priceImpact: { toFixed: () => "0.1234" } };
  }

  async getPosition(position: PublicKey): Promise<{ positionData: { totalXAmount: string } }> {
    expect(position.toBase58()).toBe(EXISTING_POSITION);
    return { positionData: { totalXAmount: "1001" } };
  }

  async initializePositionAndAddLiquidityByStrategy(parameters: {
    positionPubKey: PublicKey;
  }): Promise<Transaction> {
    this.calls.push({ operation: "OPEN", parameters });
    return new Transaction().add(
      SystemProgram.createAccount({
        fromPubkey: this.wallet.publicKey,
        newAccountPubkey: parameters.positionPubKey,
        lamports: 1,
        space: 0,
        programId: SystemProgram.programId,
      }),
    );
  }

  async addLiquidityByStrategy(parameters: Record<string, unknown>): Promise<Transaction> {
    this.calls.push({ operation: "ADD", parameters });
    return this.#transfer();
  }

  async swap(): Promise<Transaction> {
    throw new Error("a liquidity command never swaps");
  }

  async removeLiquidity(parameters: { shouldClaimAndClose: boolean }): Promise<Transaction[]> {
    this.calls.push({
      operation: parameters.shouldClaimAndClose ? "CLOSE" : "REMOVE",
      parameters,
    });
    return [this.#transfer()];
  }

  #transfer(): Transaction {
    return new Transaction().add(
      SystemProgram.transfer({
        fromPubkey: this.wallet.publicKey,
        toPubkey: this.wallet.publicKey,
        lamports: 1,
      }),
    );
  }
}

function factoryFor(pool: FakePool): MeteoraDlmmFactory {
  return {
    StrategyType: { Spot: 11, BidAsk: 22 },
    async create(_connection: Connection, address: PublicKey): Promise<MeteoraPoolClient> {
      expect(address.toBase58()).toBe(POOL);
      return pool;
    },
  };
}

function walletSigners(wallet: Keypair): SecretSignerProvider {
  return {
    async loadWalletSigner(): Promise<Keypair> {
      return wallet;
    },
  };
}

describe("official Meteora SDK boundary", () => {
  it("creates the key of a new position for an OPEN and signs its initialization with it", async () => {
    const wallet = Keypair.generate();
    const created: Keypair[] = [];
    const pool = new FakePool(wallet);
    const boundary = new OfficialMeteoraDlmmBoundary(
      {} as Connection,
      walletSigners(wallet),
      factoryFor(pool),
      () => {
        const key = Keypair.generate();
        created.push(key);
        return key;
      },
    );

    const first = await boundary.build(command(wallet, "OPEN"));
    const second = await boundary.build(command(wallet, "OPEN"));

    expect(created).toHaveLength(2);
    expect(first.positionAddress).toBe(created[0]?.publicKey.toBase58());
    expect(second.positionAddress).toBe(created[1]?.publicKey.toBase58());
    expect(first.positionAddress).not.toBe(second.positionAddress);
    expect(first.opaqueSigners).toEqual([wallet, created[0]]);
    const parameters = pool.calls[0]?.parameters as { positionPubKey: PublicKey };
    expect(parameters.positionPubKey.toBase58()).toBe(first.positionAddress);
    expect(first.command.position_address).toBeUndefined();
  });

  it("measures the swap leg of entries and exits with the official quote", async () => {
    const wallet = Keypair.generate();
    const pool = new FakePool(wallet);
    const boundary = new OfficialMeteoraDlmmBoundary(
      {} as Connection,
      walletSigners(wallet),
      factoryFor(pool),
    );

    const built = [];
    for (const operation of ["OPEN", "ADD", "REMOVE", "CLOSE"] as const) {
      built.push(await boundary.build(command(wallet, operation)));
    }

    expect(pool.calls.map((value) => value.operation)).toEqual(["OPEN", "ADD", "REMOVE", "CLOSE"]);
    expect(pool.calls[0]?.parameters.strategy).toMatchObject({ strategyType: 11 });
    expect(pool.calls[1]?.parameters.strategy).toMatchObject({ strategyType: 22 });
    expect(String(pool.calls[2]?.parameters.bps)).toBe("5000");
    // An entry buys its X leg with Y (exact out); an exit sells the withdrawn X for Y.
    // 1001 X in the position: half of it (floored) for REMOVE, all of it for CLOSE.
    expect(pool.quotes).toEqual([
      { kind: "exact-out", amount: "1000", swapForY: false, slippage: "50" },
      { kind: "exact-out", amount: "1000", swapForY: false, slippage: "50" },
      { kind: "exact-in", amount: "500", swapForY: true, slippage: "50" },
      { kind: "exact-in", amount: "1001", swapForY: true, slippage: "50" },
    ]);
    // 0.1234 % rounds up to 13 bps; 0.5 % is 50 bps.
    expect(built.map((value) => value.market.priceImpactBps)).toEqual([13, 13, 50, 50]);
    expect(built.map((value) => value.market.activeBinId)).toEqual([8001, 8001, 8001, 8001]);
    expect(built[0]?.market).toMatchObject({ tokenXDecimals: 6, tokenYDecimals: 9 });
    expect(built.slice(1).map((value) => value.positionAddress)).toEqual([
      EXISTING_POSITION,
      EXISTING_POSITION,
      EXISTING_POSITION,
    ]);
    expect(built.slice(1).map((value) => value.opaqueSigners)).toEqual([
      [wallet],
      [wallet],
      [wallet],
    ]);
  });

  it("has no swap leg for an entry without X", async () => {
    const wallet = Keypair.generate();
    const pool = new FakePool(wallet);
    const boundary = new OfficialMeteoraDlmmBoundary(
      {} as Connection,
      walletSigners(wallet),
      factoryFor(pool),
    );

    const built = await boundary.build(command(wallet, "OPEN", "0"));

    expect(built.market.priceImpactBps).toBe(0);
    expect(pool.quotes).toEqual([]);
  });

  it("fails the simulation closed when the official quote cannot measure the swap", async () => {
    const wallet = Keypair.generate();
    const pool = new FakePool(wallet);
    pool.quoteFailure = Object.assign(new Error("Insufficient liquidity in binArrays"), {
      name: "SWAP_QUOTE_INSUFFICIENT_LIQUIDITY",
    });
    const engine = new ExecutionEngine({
      sdk: new OfficialMeteoraDlmmBoundary({} as Connection, walletSigners(wallet), factoryFor(pool)),
      rpc: new SolanaRpcTransport({} as Connection),
      clock,
    });

    const result = await engine.simulate(command(wallet, "OPEN"));

    expect(result).toMatchObject({
      success: false,
      price_impact_bps: null,
      errors: [{ code: "PRICE_IMPACT_UNAVAILABLE", retryable: false }],
    });
  });

  it("converts the SDK percentage to whole bps, rounding against the trade", () => {
    expect(priceImpactBps("0")).toBe(0);
    expect(priceImpactBps("0.0001")).toBe(1);
    expect(priceImpactBps("0.12")).toBe(12);
    expect(priceImpactBps("12.3456")).toBe(1235);
    expect(priceImpactBps("150")).toBe(10_000);
    expect(() => priceImpactBps("-0.1")).toThrow();
    expect(() => priceImpactBps("NaN")).toThrow();
  });

  it("never lets the new position key reach a response, a log line or the durable state", async () => {
    const directory = await mkdtemp(join(tmpdir(), "executor-position-key-"));
    temporaryDirectories.push(directory);
    const statePath = join(directory, "execution-state.json");
    const wallet = Keypair.generate();
    const created: Keypair[] = [];
    const pool = new FakePool(wallet);
    let logs = "";
    const stream = new Writable({
      write(chunk, _encoding, callback): void {
        logs += String(chunk);
        callback();
      },
    });
    const connection = {
      async getLatestBlockhash() {
        return { blockhash: Keypair.generate().publicKey.toBase58(), lastValidBlockHeight: 99 };
      },
      async getFeeForMessage() {
        return { value: 10_000 };
      },
      async simulateTransaction() {
        return { value: { err: null, unitsConsumed: 1_200 } };
      },
      async sendRawTransaction() {
        throw new Error("timeout after broadcast");
      },
      async getSignatureStatuses() {
        return { value: [null] };
      },
    } as unknown as Connection;
    const engine = new ExecutionEngine({
      sdk: new OfficialMeteoraDlmmBoundary(connection, walletSigners(wallet), factoryFor(pool), () => {
        const key = Keypair.generate();
        created.push(key);
        return key;
      }),
      rpc: new SolanaRpcTransport(connection),
      store: new FileExecutionStateStore(statePath),
      clock,
      submissionEnabled: true,
      allowedMode: "LIVE",
      livePromotion,
    });
    const server = buildServer(
      { logger: { level: "trace", stream } },
      {
        engine,
        internalToken: "internal-token",
        modeToken: "mode-token",
        configuredMode: "LIVE",
      },
    );
    const payload = command(wallet, "OPEN");
    const headers = { "x-internal-token": "internal-token" };

    const simulation = await server.inject({ method: "POST", url: "/v1/simulations", headers, payload });
    const submit = await server.inject({
      method: "POST",
      url: "/v1/commands",
      headers: { ...headers, "x-mode-token": "mode-token", "idempotency-key": payload.idempotency_key },
      payload,
    });
    const lookup = await server.inject({
      method: "GET",
      url: `/v1/commands/${payload.command_id}`,
      headers,
    });
    await server.close();

    const [key] = created;
    expect(created).toHaveLength(1);
    expect(simulation.json()).toMatchObject({ success: true, position_address: key?.publicKey.toBase58() });
    expect(submit.json()).toMatchObject({ position_address: key?.publicKey.toBase58() });
    expect(lookup.json()).toMatchObject({ position_address: key?.publicKey.toBase58() });
    const secret = Buffer.from(key?.secretKey ?? []);
    const seed = secret.subarray(0, 32);
    const leaks = [
      JSON.stringify([...secret]),
      [...seed].join(","),
      secret.toString("hex"),
      seed.toString("hex"),
      secret.toString("base64"),
      seed.toString("base64"),
    ];
    const outputs = [simulation.body, submit.body, lookup.body, logs, await readFile(statePath, "utf8")];
    expect(logs.length).toBeGreaterThan(0);
    for (const output of outputs) {
      for (const leak of leaks) {
        expect(output.includes(leak)).toBe(false);
      }
    }
  });

  it("durably records the signed transaction before a timeout-after-broadcast", async () => {
    const directory = await mkdtemp(join(tmpdir(), "executor-derived-signature-"));
    temporaryDirectories.push(directory);
    const statePath = join(directory, "execution-state.json");
    const wallet = Keypair.generate();
    const value = command(wallet, "ADD");
    let sendCalls = 0;
    let statusCalls = 0;
    let reconciledSignature: string | undefined;
    const connection = {
      async getLatestBlockhash() {
        return {
          blockhash: Keypair.generate().publicKey.toBase58(),
          lastValidBlockHeight: 123,
        };
      },
      async getFeeForMessage() {
        return { value: 5_000 };
      },
      async simulateTransaction() {
        return { value: { err: null, unitsConsumed: 900 } };
      },
      async sendRawTransaction() {
        sendCalls += 1;
        throw new Error("timeout after broadcast");
      },
      async getSignatureStatuses(signatures: string[]) {
        statusCalls += 1;
        [reconciledSignature] = signatures;
        return {
          value: [
            {
              slot: 42,
              err: null,
              confirmations: null,
              confirmationStatus: "confirmed",
            },
          ],
        };
      },
    } as unknown as Connection;
    const sdk: SdkBoundary = {
      async build(builtCommand) {
        const transaction = new Transaction().add(
          SystemProgram.transfer({
            fromPubkey: wallet.publicKey,
            toPubkey: wallet.publicKey,
            lamports: 1,
          }),
        );
        return {
          command: builtCommand,
          opaqueTransactions: [transaction],
          opaqueSigners: [wallet],
          positionAddress: EXISTING_POSITION,
          market: { priceImpactBps: 10, activeBinId: 8000, tokenXDecimals: 6, tokenYDecimals: 9 },
        };
      },
    };
    const rpc = new SolanaRpcTransport(connection);
    const first = new ExecutionEngine({
      sdk,
      rpc,
      store: new FileExecutionStateStore(statePath),
      clock,
      submissionEnabled: true,
      allowedMode: "LIVE",
      livePromotion,
    });
    await first.simulate(value);
    const uncertain = await first.submit(value, value.idempotency_key, {
      modeTokenVerified: true,
    });

    const restarted = new ExecutionEngine({
      sdk,
      rpc,
      store: new FileExecutionStateStore(statePath),
      clock,
      submissionEnabled: true,
      allowedMode: "LIVE",
      livePromotion,
    });
    const reconciled = await restarted.submit(value, value.idempotency_key, {
      modeTokenVerified: true,
    });

    expect(uncertain).toMatchObject({ state: "UNKNOWN", transaction_count: 1 });
    expect(uncertain.signature).toBeTruthy();
    expect(uncertain.signatures).toEqual([uncertain.signature]);
    expect(reconciled.state).toBe("CONFIRMED");
    expect(reconciledSignature).toBe(uncertain.signature);
    expect(sendCalls).toBe(1);
    expect(statusCalls).toBe(1);
  });

  it("redacts the secret path and file contents when signer loading fails", async () => {
    const directory = await mkdtemp(join(tmpdir(), "executor-signer-"));
    temporaryDirectories.push(directory);
    const path = join(directory, "wallet-private-material.json");
    await writeFile(path, "not-valid-secret-material", "utf8");
    const provider = new FileSecretSignerProvider(path);

    await expect(provider.loadWalletSigner()).rejects.toThrow(
      "secret signer is unavailable or invalid",
    );
    await expect(provider.loadWalletSigner()).rejects.not.toThrow(path);
    await expect(provider.loadWalletSigner()).rejects.not.toThrow("not-valid-secret-material");
  });
});

describe("official SDK boundary: exit quotes and transaction failures", () => {
  it("still builds an exit whose swap cannot be quoted, with an unknown impact", async () => {
    const wallet = Keypair.generate();
    const pool = new FakePool(wallet);
    pool.quoteFailure = Object.assign(new Error("Insufficient liquidity in binArrays"), {
      name: "SWAP_QUOTE_INSUFFICIENT_LIQUIDITY",
    });
    const boundary = new OfficialMeteoraDlmmBoundary(
      {} as Connection,
      walletSigners(wallet),
      factoryFor(pool),
    );

    const built = await boundary.build(command(wallet, "CLOSE"));

    expect(built.market.priceImpactBps).toBeNull();
    expect(built.opaqueTransactions).toHaveLength(1);
  });

  it("names a transaction the wallet cannot fund apart from other failures", async () => {
    const wallet = Keypair.generate();
    const simulateWith = (logs: string[]): Promise<{ errors: Array<{ code: string }> }> => {
      const connection = {
        async getLatestBlockhash() {
          return { blockhash: Keypair.generate().publicKey.toBase58(), lastValidBlockHeight: 9 };
        },
        async getFeeForMessage() {
          return { value: 5_000 };
        },
        async simulateTransaction() {
          return { value: { err: { InstructionError: [0, { Custom: 1 }] }, logs, unitsConsumed: 0 } };
        },
      } as unknown as Connection;
      const engine = new ExecutionEngine({
        sdk: new OfficialMeteoraDlmmBoundary(connection, walletSigners(wallet), factoryFor(new FakePool(wallet))),
        rpc: new SolanaRpcTransport(connection),
        clock,
      });
      return engine.simulate(command(wallet, "ADD"));
    };

    const unfunded = await simulateWith(["Program log: Error: insufficient funds"]);
    const other = await simulateWith(["Program log: custom program error: 0x1771"]);

    expect(unfunded.errors.map((error) => error.code)).toEqual(["INSUFFICIENT_FUNDS"]);
    expect(other.errors.map((error) => error.code)).toEqual(["TRANSACTION_SIMULATION_FAILED"]);
  });
});
