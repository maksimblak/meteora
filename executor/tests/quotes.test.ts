import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { Connection, PublicKey } from "@solana/web3.js";
import { afterEach, describe, expect, it } from "vitest";

import { QuoteResultV1Schema, parseQuoteRequestV1 } from "../src/contracts.js";
import {
  InsufficientLiquidityError,
  QuoteService,
  type PoolQuote,
  type PoolQuoter,
} from "../src/quotes.js";
import {
  OfficialMeteoraQuoter,
  type MeteoraDlmmFactory,
  type MeteoraPoolClient,
  type SwapQuote,
} from "../src/sdk.js";
import { buildServer } from "../src/server.js";

const clock = { now: (): Date => new Date("2030-01-02T03:00:00.000Z") };
const corpus = JSON.parse(
  readFileSync(
    fileURLToPath(new URL("../../contracts/execution/quote-v1-corpus.json", import.meta.url)),
    "utf8",
  ),
) as { valid_requests: unknown[]; valid_results: unknown[] };
const request = parseQuoteRequestV1(corpus.valid_requests[0]);
const servers: Array<ReturnType<typeof buildServer>> = [];
// The corpus results as this executor serializes them (millisecond UTC timestamps).
const expected = corpus.valid_results.map((value) => ({
  ...QuoteResultV1Schema.parse(value),
  quoted_at: clock.now().toISOString(),
}));

afterEach(async () => {
  await Promise.all(servers.splice(0).map((server) => server.close()));
});

function fixedQuoter(): PoolQuoter & { calls: Array<[string, boolean, bigint]> } {
  const calls: Array<[string, boolean, bigint]> = [];
  return {
    calls,
    async quote(pool: string, swapForY: boolean, inAmount: bigint): Promise<PoolQuote> {
      calls.push([pool, swapForY, inAmount]);
      return {
        consumedInAmount: "1000000000",
        outAmount: "498000000",
        feeAmount: "2000000",
        priceImpactBps: 37,
        activeBinId: 8001,
        tokenXDecimals: 6,
        tokenYDecimals: 9,
      };
    },
  };
}

describe("read-only pool quotes", () => {
  it("returns the official quote of a reference size as the shared V1 result", async () => {
    const quoter = fixedQuoter();

    const result = await new QuoteService(quoter, clock).quote(request);

    expect(quoter.calls).toEqual([[request.pool_address, false, 1_000_000_000n]]);
    expect(result).toEqual(expected[0]);
  });

  it("answers a pool too thin for the size and never invents a value for a failed quote", async () => {
    const thin: PoolQuoter = {
      async quote() {
        throw new InsufficientLiquidityError({
          activeBinId: 8001,
          tokenXDecimals: 6,
          tokenYDecimals: 9,
        });
      },
    };
    const broken: PoolQuoter = {
      async quote() {
        throw new Error("rpc unavailable");
      },
    };
    const reverse = parseQuoteRequestV1(corpus.valid_requests[1]);

    const thinResult = await new QuoteService(thin, clock).quote(reverse);
    const brokenResult = await new QuoteService(broken, clock).quote(request);

    expect(thinResult).toEqual(expected[1]);
    expect(brokenResult).toMatchObject({
      success: false,
      price_impact_bps: null,
      out_amount: null,
      error: { code: "QUOTE_FAILED", retryable: true },
    });
  });

  it("measures the quote with the official SDK exact-in quote, without any wallet", async () => {
    const seen: Array<{ amount: string; swapForY: boolean; slippage: string }> = [];
    const pool = {
      lbPair: { activeId: 8001 },
      tokenX: { mint: { decimals: 6 } },
      tokenY: { mint: { decimals: 9 } },
      async getBinArrayForSwap(): Promise<unknown[]> {
        return [];
      },
      swapQuote(
        inAmount: { toString(): string },
        swapForY: boolean,
        slippage: { toString(): string },
      ): SwapQuote {
        seen.push({ amount: inAmount.toString(), swapForY, slippage: slippage.toString() });
        return {
          consumedInAmount: { toString: (): string => "1000000000" },
          outAmount: { toString: (): string => "498000000" },
          fee: { toString: (): string => "2000000" },
          priceImpact: { toFixed: (): string => "0.3612" },
        };
      },
    } as unknown as MeteoraPoolClient;
    const factory: MeteoraDlmmFactory = {
      StrategyType: { Spot: 0, BidAsk: 2 },
      async create(_connection: Connection, address: PublicKey) {
        expect(address.toBase58()).toBe(request.pool_address);
        return pool;
      },
    };

    const quote = await new OfficialMeteoraQuoter({} as Connection, factory).quote(
      request.pool_address,
      false,
      1_000_000_000n,
    );

    expect(seen).toEqual([{ amount: "1000000000", swapForY: false, slippage: "0" }]);
    expect(quote).toEqual({
      consumedInAmount: "1000000000",
      outAmount: "498000000",
      feeAmount: "2000000",
      priceImpactBps: 37,
      activeBinId: 8001,
      tokenXDecimals: 6,
      tokenYDecimals: 9,
    });
  });

  it("serves POST /v1/quotes in SHADOW without wallet material, behind the internal token", async () => {
    const server = buildServer(
      { logger: false },
      {
        configuredMode: "SHADOW",
        internalToken: "internal-token",
        quotes: new QuoteService(fixedQuoter(), clock),
      },
    );
    const unquoted = buildServer({ logger: false }, { internalToken: "internal-token" });
    servers.push(server, unquoted);
    const headers = { "x-internal-token": "internal-token" };

    const anonymous = await server.inject({ method: "POST", url: "/v1/quotes", payload: request });
    const quoted = await server.inject({ method: "POST", url: "/v1/quotes", headers, payload: request });
    const retired = await server.inject({
      method: "POST",
      url: "/v1/quotes",
      headers,
      payload: { ...request, schema_version: 2 },
    });
    const invalid = await server.inject({
      method: "POST",
      url: "/v1/quotes",
      headers,
      payload: { ...request, in_amount: "0" },
    });
    const unavailable = await unquoted.inject({
      method: "POST",
      url: "/v1/quotes",
      headers,
      payload: request,
    });

    expect(anonymous.statusCode).toBe(401);
    expect(quoted.statusCode).toBe(200);
    expect(quoted.json()).toEqual(expected[0]);
    expect([retired.statusCode, retired.json()]).toEqual([
      400,
      { error: "unsupported_contract_version" },
    ]);
    expect([invalid.statusCode, invalid.json()]).toEqual([400, { error: "invalid_contract" }]);
    expect([unavailable.statusCode, unavailable.json()]).toEqual([
      503,
      { error: "quotes_unavailable" },
    ]);
  });
});
