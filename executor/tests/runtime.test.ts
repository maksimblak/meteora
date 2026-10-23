import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { Keypair } from "@solana/web3.js";
import { afterEach, describe, expect, it } from "vitest";

import { buildRuntimeConfig, buildServer } from "../src/server.js";

const temporaryDirectories: string[] = [];
// Nothing listens here: the RPC probe must fail fast and keep money modes unready.
const UNREACHABLE_RPC = "http://127.0.0.1:9";

afterEach(async () => {
  await Promise.all(temporaryDirectories.splice(0).map((path) => rm(path, { recursive: true })));
});

async function promotionFile(directory: string, promotedAt: string, expiresAt: string): Promise<string> {
  const path = join(directory, "live-promotion.json");
  await writeFile(
    path,
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
      promotedAt,
      expiresAt,
    }),
    "utf8",
  );
  return path;
}

describe("executor runtime composition", () => {
  it("starts SHADOW without RPC or wallet material", async () => {
    const config = await buildRuntimeConfig({ TRADING_MODE: "SHADOW" });

    expect(config.configuredMode).toBe("SHADOW");
    expect(config.engine).toBeUndefined();
    expect(config.quotes).toBeUndefined();
    expect(config.liveReadiness).toBeUndefined();
  });

  it("serves read-only quotes in SHADOW with an RPC and no wallet", async () => {
    const config = await buildRuntimeConfig({
      TRADING_MODE: "SHADOW",
      SOLANA_RPC_URL: UNREACHABLE_RPC,
      EXECUTOR_INTERNAL_TOKEN: "internal-token",
    });

    expect(config.quotes).toBeDefined();
    expect(config.engine).toBeUndefined();
    const server = buildServer({ logger: false }, config);
    const readiness = await server.inject({ method: "GET", url: "/health/ready" });
    await server.close();
    expect(readiness.json()).toEqual({ status: "ready", mode: "SHADOW" });
  });

  it("constructs LIVE but stays unready while its RPC cannot be reached", async () => {
    const directory = await mkdtemp(join(tmpdir(), "executor-runtime-"));
    temporaryDirectories.push(directory);
    const walletPath = join(directory, "wallet.json");
    const statePath = join(directory, "execution-state.json");
    const promotionPath = await promotionFile(
      directory,
      "2026-10-18T11:00:00.000Z",
      "2030-01-02T03:04:05.000Z",
    );
    const wallet = Keypair.generate();
    await writeFile(walletPath, JSON.stringify([...wallet.secretKey]), "utf8");
    const environ = {
      TRADING_MODE: "LIVE",
      SOLANA_RPC_URL: UNREACHABLE_RPC,
      WALLET_KEYPAIR_FILE: walletPath,
      WALLET_PUBLIC_KEY: wallet.publicKey.toBase58(),
      EXECUTOR_STATE_FILE: statePath,
      LIVE_PROMOTION_FILE: promotionPath,
      EXECUTOR_INTERNAL_TOKEN: "internal-token",
      EXECUTOR_MODE_TOKEN: "mode-token",
    };

    const configured = await buildRuntimeConfig(environ);
    const missingWallet = await buildRuntimeConfig({
      ...environ,
      WALLET_KEYPAIR_FILE: join(directory, "missing.json"),
    });

    expect(configured.engine).toBeDefined();
    expect(configured.liveReadiness).toBeDefined();
    expect(missingWallet.engine).toBeUndefined();

    const server = buildServer({ logger: false }, configured);
    const readiness = await server.inject({ method: "GET", url: "/health/ready" });
    await server.close();
    expect(readiness.statusCode).toBe(503);
    expect(readiness.json()).toEqual({ status: "not_ready", missing: ["rpc"] });
  });

  it("keeps LIVE readiness false for future-dated promotion evidence", async () => {
    const directory = await mkdtemp(join(tmpdir(), "executor-future-promotion-"));
    temporaryDirectories.push(directory);
    const walletPath = join(directory, "wallet.json");
    const promotionPath = await promotionFile(
      directory,
      "2099-01-01T00:00:00.000Z",
      "2100-01-01T00:00:00.000Z",
    );
    const wallet = Keypair.generate();
    await writeFile(walletPath, JSON.stringify([...wallet.secretKey]), "utf8");

    const config = await buildRuntimeConfig({
      TRADING_MODE: "LIVE",
      SOLANA_RPC_URL: UNREACHABLE_RPC,
      WALLET_KEYPAIR_FILE: walletPath,
      EXECUTOR_STATE_FILE: join(directory, "execution-state.json"),
      LIVE_PROMOTION_FILE: promotionPath,
      EXECUTOR_INTERNAL_TOKEN: "internal-token",
      EXECUTOR_MODE_TOKEN: "mode-token",
    });

    expect(config.engine).toBeUndefined();
    const server = buildServer({ logger: false }, config);
    const readiness = await server.inject({ method: "GET", url: "/health/ready" });
    await server.close();
    expect(readiness.statusCode).toBe(503);
  });
});
