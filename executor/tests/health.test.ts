import { afterEach, describe, expect, it } from "vitest";

import { ExecutionEngine, type RpcTransport, type SdkBoundary } from "../src/engine.js";
import type { LiveReadiness } from "../src/sdk.js";
import { buildServer } from "../src/server.js";

const servers: Array<ReturnType<typeof buildServer>> = [];

afterEach(async () => {
  await Promise.all(servers.splice(0).map((server) => server.close()));
});

const unusedSdk: SdkBoundary = {
  async build() {
    throw new Error("not used");
  },
};
const unusedRpc: RpcTransport = {
  async simulate() {
    throw new Error("not used");
  },
  async submit() {
    throw new Error("not used");
  },
  async status() {
    throw new Error("not used");
  },
};

function liveServer(readiness: LiveReadiness | Error): ReturnType<typeof buildServer> {
  const server = buildServer(
    { logger: false },
    {
      engine: new ExecutionEngine({ sdk: unusedSdk, rpc: unusedRpc, allowedMode: "LIVE" }),
      internalToken: "internal-token",
      modeToken: "mode-token",
      configuredMode: "LIVE",
      liveReadiness: {
        async check() {
          if (readiness instanceof Error) {
            throw readiness;
          }
          return readiness;
        },
      },
    },
  );
  servers.push(server);
  return server;
}

describe("executor health", () => {
  it("is live but not ready before execution dependencies are configured", async () => {
    const server = buildServer({ logger: false });
    servers.push(server);

    const live = await server.inject({ method: "GET", url: "/health/live" });
    const ready = await server.inject({ method: "GET", url: "/health/ready" });

    expect(live.statusCode).toBe(200);
    expect(live.json()).toEqual({ status: "ok" });
    expect(ready.statusCode).toBe(503);
    expect(ready.json()).toEqual({ status: "not_ready", missing: ["configuration"] });
  });

  it("is ready for LIVE only when the wallet, the RPC and price impact quotes are available", async () => {
    const all = { wallet: true, rpc: true, priceImpact: true };

    const ready = await liveServer(all).inject({ method: "GET", url: "/health/ready" });
    const answers = await Promise.all(
      [
        { ...all, wallet: false },
        { ...all, rpc: false },
        { ...all, priceImpact: false },
        new Error("probe failed"),
      ].map((state) => liveServer(state).inject({ method: "GET", url: "/health/ready" })),
    );

    expect(ready.statusCode).toBe(200);
    expect(ready.json()).toEqual({ status: "ready", mode: "LIVE" });
    expect(answers.map((answer) => answer.statusCode)).toEqual([503, 503, 503, 503]);
    expect(answers.map((answer) => answer.json().missing)).toEqual([
      ["wallet"],
      ["rpc"],
      ["price_impact"],
      ["wallet", "rpc", "price_impact"],
    ]);
  });

  it("keeps LIVE unready without an engine even when the probe passes", async () => {
    const server = buildServer(
      { logger: false },
      {
        configuredMode: "LIVE",
        internalToken: "internal-token",
        modeToken: "mode-token",
        liveReadiness: {
          async check() {
            return { wallet: true, rpc: true, priceImpact: true };
          },
        },
      },
    );
    servers.push(server);

    const ready = await server.inject({ method: "GET", url: "/health/ready" });

    expect(ready.statusCode).toBe(503);
    expect(ready.json()).toEqual({ status: "not_ready", missing: ["configuration"] });
  });
});
