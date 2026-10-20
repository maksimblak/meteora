import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { Keypair } from "@solana/web3.js";
import { afterEach, describe, expect, it } from "vitest";

import { buildRuntimeConfig, buildServer } from "../src/server.js";

const temporaryDirectories: string[] = [];

afterEach(async () => {
  await Promise.all(temporaryDirectories.splice(0).map((path) => rm(path, { recursive: true })));
});

describe("executor runtime composition", () => {
  it("starts SHADOW without RPC or wallet material", async () => {
    const config = await buildRuntimeConfig({ TRADING_MODE: "SHADOW" });

    expect(config).toMatchObject({
      configuredMode: "SHADOW",
      liveDependenciesReady: false,
    });
    expect(config.engine).toBeUndefined();
  });

  it("constructs LIVE diagnostics but stays unready without measured price impact", async () => {
    const directory = await mkdtemp(join(tmpdir(), "executor-runtime-"));
    temporaryDirectories.push(directory);
    const walletPath = join(directory, "wallet.json");
    const statePath = join(directory, "execution-state.json");
    const promotionPath = join(directory, "live-promotion.json");
    const wallet = Keypair.generate();
    await writeFile(walletPath, JSON.stringify([...wallet.secretKey]), "utf8");
    await writeFile(
      promotionPath,
      JSON.stringify({
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
        expiresAt: "2030-01-02T03:04:05.000Z",
      }),
      "utf8",
    );

    const ready = await buildRuntimeConfig({
      TRADING_MODE: "LIVE",
      SOLANA_RPC_URL: "http://127.0.0.1:8899",
      WALLET_KEYPAIR_FILE: walletPath,
      EXECUTOR_STATE_FILE: statePath,
      LIVE_PROMOTION_FILE: promotionPath,
      EXECUTOR_INTERNAL_TOKEN: "internal-token",
      EXECUTOR_MODE_TOKEN: "mode-token",
    });
    const missingWallet = await buildRuntimeConfig({
      TRADING_MODE: "LIVE",
      SOLANA_RPC_URL: "http://127.0.0.1:8899",
      WALLET_KEYPAIR_FILE: join(directory, "missing.json"),
      EXECUTOR_STATE_FILE: statePath,
      LIVE_PROMOTION_FILE: promotionPath,
      EXECUTOR_INTERNAL_TOKEN: "internal-token",
      EXECUTOR_MODE_TOKEN: "mode-token",
    });

    expect(ready.engine).toBeDefined();
    expect(ready.liveDependenciesReady).toBe(false);
    expect(missingWallet.engine).toBeUndefined();
    expect(missingWallet.liveDependenciesReady).toBe(false);

    const server = buildServer({ logger: false }, ready);
    const readiness = await server.inject({ method: "GET", url: "/health/ready" });
    await server.close();
    expect(readiness.statusCode).toBe(503);
    expect(readiness.json()).toEqual({ status: "not_ready" });
  });

  it("keeps LIVE readiness false for future-dated promotion evidence", async () => {
    const directory = await mkdtemp(join(tmpdir(), "executor-future-promotion-"));
    temporaryDirectories.push(directory);
    const walletPath = join(directory, "wallet.json");
    const promotionPath = join(directory, "live-promotion.json");
    const wallet = Keypair.generate();
    await writeFile(walletPath, JSON.stringify([...wallet.secretKey]), "utf8");
    await writeFile(
      promotionPath,
      JSON.stringify({
        promotionId: "50000000-0000-4000-8000-000000000001",
        rulesetVersion: "rules-2026-10-18",
        approvalCycles: 10,
        receiptsReconciled: true,
        riskInvariantsPreserved: true,
        dedicatedRpcReady: true,
        walletReady: true,
        checklistCompleted: true,
        operatorConfirmationId: "operator-confirmation-1",
        promotedAt: "2099-01-01T00:00:00.000Z",
        expiresAt: "2100-01-01T00:00:00.000Z",
      }),
      "utf8",
    );

    const config = await buildRuntimeConfig({
      TRADING_MODE: "LIVE",
      SOLANA_RPC_URL: "http://127.0.0.1:8899",
      WALLET_KEYPAIR_FILE: walletPath,
      EXECUTOR_STATE_FILE: join(directory, "execution-state.json"),
      LIVE_PROMOTION_FILE: promotionPath,
      EXECUTOR_INTERNAL_TOKEN: "internal-token",
      EXECUTOR_MODE_TOKEN: "mode-token",
    });

    expect(config.engine).toBeUndefined();
    expect(config.liveDependenciesReady).toBe(false);
  });
});
