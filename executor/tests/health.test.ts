import { afterEach, describe, expect, it } from "vitest";

import { buildServer } from "../src/server.js";

const servers: Array<ReturnType<typeof buildServer>> = [];

afterEach(async () => {
  await Promise.all(servers.splice(0).map((server) => server.close()));
});

describe("executor health", () => {
  it("is live but not ready before execution dependencies are configured", async () => {
    const server = buildServer({ logger: false });
    servers.push(server);

    const live = await server.inject({ method: "GET", url: "/health/live" });
    const ready = await server.inject({ method: "GET", url: "/health/ready" });

    expect(live.statusCode).toBe(200);
    expect(live.json()).toEqual({ status: "ok" });
    expect(ready.statusCode).toBe(503);
    expect(ready.json()).toEqual({ status: "not_ready" });
  });
});
