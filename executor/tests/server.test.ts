import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { afterEach, describe, expect, it } from "vitest";

import { parseDlmmCommandV2, type DlmmCommandV2 } from "../src/contracts.js";
import {
  ExecutionEngine,
  type LivePromotionEvidence,
  type BuiltCommand,
  type RpcTransport,
  type SdkBoundary,
} from "../src/engine.js";
import { buildServer } from "../src/server.js";

const servers: Array<ReturnType<typeof buildServer>> = [];
const commandId = "10000000-0000-4000-8000-000000000001";
const POSITION = "SysvarRent111111111111111111111111111111111";
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
  expiresAt: "2030-01-02T03:04:05.000Z",
};
const payload = parseDlmmCommandV2({
  schema_version: 2,
  command_id: commandId,
  idempotency_key: "plan-101-open-v1",
  correlation_id: "20000000-0000-4000-8000-000000000001",
  ruleset_version: "rules-2026-10-18",
  mode: "LIVE",
  operation: "OPEN",
  pool_address: "11111111111111111111111111111111",
  wallet_address: "11111111111111111111111111111111",
  expires_at: "2030-01-02T03:04:05Z",
  max_slippage_bps: 50,
  strategy: "SPOT",
  min_bin_id: -4,
  max_bin_id: 7,
  amount_x: "1000",
  amount_y: "2000",
  active_bin_id: 1,
  active_bin_tolerance: 2,
  token_x_decimals: 6,
  token_y_decimals: 6,
});
const v1Command = (
  JSON.parse(
    readFileSync(
      fileURLToPath(new URL("../../contracts/execution/v1-corpus.json", import.meta.url)),
      "utf8",
    ),
  ) as { valid_commands: unknown[] }
).valid_commands[0];

class FakeSdk implements SdkBoundary {
  async build(command: DlmmCommandV2): Promise<BuiltCommand> {
    return {
      command,
      opaqueTransactions: [{}],
      positionAddress: POSITION,
      market: { priceImpactBps: 10, activeBinId: 1, tokenXDecimals: 6, tokenYDecimals: 6 },
    };
  }
}

class FakeRpc implements RpcTransport {
  statusCalls = 0;
  simulateCalls = 0;

  async simulate(): Promise<{
    computeUnits: number;
    networkFeeLamports: bigint;
    priorityFeeLamports: bigint;
    blockhash: string;
    lastValidBlockHeight: number;
    logs: string[];
  }> {
    this.simulateCalls += 1;
    return {
      computeUnits: 900,
      networkFeeLamports: 5000n,
      priorityFeeLamports: 0n,
      blockhash: "safe-blockhash",
      lastValidBlockHeight: 123,
      logs: [],
    };
  }

  async submit(): Promise<string> {
    return "signature-1";
  }

  async status(): Promise<{ state: "CONFIRMED"; slot: number }> {
    this.statusCalls += 1;
    return { state: "CONFIRMED", slot: 42 };
  }
}

afterEach(async () => {
  await Promise.all(servers.splice(0).map((server) => server.close()));
});

function liveServer(rpc: FakeRpc): ReturnType<typeof buildServer> {
  const engine = new ExecutionEngine({
    sdk: new FakeSdk(),
    rpc,
    clock: { now: (): Date => new Date("2026-10-18T12:00:00Z") },
    submissionEnabled: true,
    allowedMode: "LIVE",
    livePromotion,
  });
  const server = buildServer(
    { logger: false },
    {
      engine,
      internalToken: "internal-token",
      modeToken: "mode-token",
      configuredMode: "LIVE",
    },
  );
  servers.push(server);
  return server;
}

describe("executor HTTP boundary", () => {
  it("rejects commands without the internal token before parsing or submitting", async () => {
    const server = buildServer({ logger: false }, { internalToken: "configured-in-memory" });
    servers.push(server);

    const response = await server.inject({
      method: "POST",
      url: "/v1/commands",
      payload: { schema_version: 1 },
    });

    expect(response.statusCode).toBe(401);
    expect(response.json()).toEqual({ error: "unauthorized" });
  });

  it("keeps shadow readiness independent from wallet and live RPC", async () => {
    const server = buildServer(
      { logger: false },
      { internalToken: "configured-in-memory", configuredMode: "SHADOW" },
    );
    servers.push(server);

    const response = await server.inject({ method: "GET", url: "/health/ready" });

    expect(response.statusCode).toBe(200);
    expect(response.json()).toEqual({ status: "ready", mode: "SHADOW" });
  });

  it("rejects a retired V1 command explicitly and never simulates or submits it", async () => {
    const rpc = new FakeRpc();
    const server = liveServer(rpc);
    const headers = { "x-internal-token": "internal-token" };

    const simulation = await server.inject({
      method: "POST",
      url: "/v1/simulations",
      headers,
      payload: v1Command as Record<string, unknown>,
    });
    const submit = await server.inject({
      method: "POST",
      url: "/v1/commands",
      headers: { ...headers, "x-mode-token": "mode-token", "idempotency-key": "plan-101-open-v1" },
      payload: v1Command as Record<string, unknown>,
    });

    for (const response of [simulation, submit]) {
      expect([response.statusCode, response.json()]).toEqual([
        400,
        { error: "unsupported_contract_version" },
      ]);
    }
    expect(rpc.simulateCalls).toBe(0);
  });

  it("reconciles command status through RPC without resubmitting", async () => {
    const rpc = new FakeRpc();
    const server = liveServer(rpc);
    const headers = { "x-internal-token": "internal-token" };

    expect(
      (await server.inject({ method: "POST", url: "/v1/simulations", headers, payload })).statusCode,
    ).toBe(200);
    expect(
      (
        await server.inject({
          method: "POST",
          url: "/v1/commands",
          headers: {
            ...headers,
            "x-mode-token": "mode-token",
            "idempotency-key": payload.idempotency_key,
          },
          payload,
        })
      ).statusCode,
    ).toBe(202);

    const response = await server.inject({
      method: "GET",
      url: `/v1/commands/${commandId}`,
      headers,
    });

    expect(response.statusCode).toBe(200);
    expect(response.json()).toMatchObject({
      schema_version: 2,
      state: "CONFIRMED",
      signature: "signature-1",
      position_address: POSITION,
    });
    expect(rpc.statusCalls).toBe(1);
  });
});
