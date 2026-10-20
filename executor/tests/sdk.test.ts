import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { Keypair, SystemProgram, Transaction, type Connection } from "@solana/web3.js";
import { afterEach, describe, expect, it } from "vitest";

import { parseDlmmCommandV1, type DlmmCommandV1 } from "../src/contracts.js";
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
  type MeteoraDlmmFactory,
  type MeteoraPoolClient,
  type SecretSignerProvider,
} from "../src/sdk.js";

const temporaryDirectories: string[] = [];

afterEach(async () => {
  await Promise.all(temporaryDirectories.splice(0).map((path) => rm(path, { recursive: true })));
});

function command(
  wallet: Keypair,
  position: Keypair,
  operation: "OPEN" | "ADD" | "REMOVE" | "CLOSE",
): DlmmCommandV1 {
  const addsLiquidity = operation === "OPEN" || operation === "ADD";
  return parseDlmmCommandV1({
    schema_version: 1,
    command_id: `10000000-0000-4000-8000-00000000000${operation.length}`,
    idempotency_key: `operation-${operation.toLowerCase()}`,
    correlation_id: "20000000-0000-4000-8000-000000000001",
    ruleset_version: "rules-2026-10-18",
    mode: "LIVE",
    operation,
    pool_address: "11111111111111111111111111111111",
    wallet_address: wallet.publicKey.toBase58(),
    position_address: position.publicKey.toBase58(),
    expires_at: "2030-01-02T03:04:05Z",
    max_slippage_bps: 50,
    min_bin_id: -4,
    max_bin_id: 7,
    ...(addsLiquidity
      ? {
          strategy: operation === "ADD" ? "BID_ASK" : "SPOT",
          amount_x: "1000",
          amount_y: "2000",
        }
      : { remove_bps: operation === "CLOSE" ? 10_000 : 5_000 }),
  });
}

describe("official Meteora SDK boundary", () => {
  it("builds open, add, remove and close without exposing SDK values in the contract", async () => {
    const wallet = Keypair.generate();
    const position = Keypair.generate();
    const calls: Array<{ operation: string; parameters: Record<string, unknown> }> = [];
    const pool: MeteoraPoolClient = {
      async initializePositionAndAddLiquidityByStrategy(parameters) {
        calls.push({ operation: "OPEN", parameters });
        return new Transaction();
      },
      async addLiquidityByStrategy(parameters) {
        calls.push({ operation: "ADD", parameters });
        return new Transaction();
      },
      async removeLiquidity(parameters) {
        calls.push({
          operation: parameters.shouldClaimAndClose ? "CLOSE" : "REMOVE",
          parameters,
        });
        return [new Transaction()];
      },
    };
    const factory: MeteoraDlmmFactory = {
      StrategyType: { Spot: 11, BidAsk: 22 },
      async create() {
        return pool;
      },
    };
    const signers: SecretSignerProvider = {
      async loadWalletSigner() {
        return wallet;
      },
      async loadPositionSigner() {
        return position;
      },
    };
    const boundary = new OfficialMeteoraDlmmBoundary({} as Connection, signers, factory);

    const built = [];
    for (const operation of ["OPEN", "ADD", "REMOVE", "CLOSE"] as const) {
      built.push(await boundary.build(command(wallet, position, operation)));
    }

    expect(calls.map((value) => value.operation)).toEqual(["OPEN", "ADD", "REMOVE", "CLOSE"]);
    expect(calls[0]?.parameters.strategy).toMatchObject({ strategyType: 11 });
    expect(calls[1]?.parameters.strategy).toMatchObject({ strategyType: 22 });
    expect(String(calls[2]?.parameters.bps)).toBe("5000");
    expect(calls[3]?.parameters.shouldClaimAndClose).toBe(true);
    expect(built.map((value) => value.opaqueTransactions.length)).toEqual([1, 1, 1, 1]);
    expect(built[0]?.opaqueSigners).toEqual([wallet, position]);
    expect(built.every((value) => value.priceImpactBps === undefined)).toBe(true);
    expect(built.slice(1).map((value) => value.opaqueSigners)).toEqual([
      [wallet],
      [wallet],
      [wallet],
    ]);
  });

  it("fails closed when the official runtime has no measured price impact", async () => {
    const wallet = Keypair.generate();
    const position = Keypair.generate();
    const transport = new SolanaRpcTransport({} as Connection);

    await expect(
      transport.simulate({
        command: command(wallet, position, "ADD"),
        opaqueTransactions: [new Transaction()],
        opaqueSigners: [wallet],
      }),
    ).rejects.toThrow("measured price impact is unavailable");
  });

  it("durably records the signed transaction before a timeout-after-broadcast", async () => {
    const directory = await mkdtemp(join(tmpdir(), "executor-derived-signature-"));
    temporaryDirectories.push(directory);
    const statePath = join(directory, "execution-state.json");
    const wallet = Keypair.generate();
    const position = Keypair.generate();
    const value = command(wallet, position, "ADD");
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
          priceImpactBps: 10,
        };
      },
    };
    const rpc = new SolanaRpcTransport(connection);
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
    const provider = new FileSecretSignerProvider(path, directory);

    await expect(provider.loadWalletSigner()).rejects.toThrow(
      "secret signer is unavailable or invalid",
    );
    await expect(provider.loadWalletSigner()).rejects.not.toThrow(path);
    await expect(provider.loadWalletSigner()).rejects.not.toThrow("not-valid-secret-material");
  });
});
