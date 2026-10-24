import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  ChainReceiptV1Schema,
  ContractVersionError,
  DlmmCommandV2Schema,
  GatewayReceiptV2Schema,
  QuoteRequestV1Schema,
  QuoteResultV1Schema,
  SimulationResultV2Schema,
  parseDlmmCommandV2,
  parseGatewayReceiptV2,
  parseQuoteRequestV1,
  parseSimulationResultV2,
} from "../src/contracts.js";
import { commandDigest, simulationDigest } from "../src/engine.js";

type Cases = Array<{ case: string; payload: unknown }>;

type ExecutionCorpus = {
  valid_commands: unknown[];
  canonical_command_digests: string[];
  invalid_commands: Cases;
  valid_simulations: unknown[];
  canonical_simulation_digests: string[];
  invalid_simulations: Cases;
  valid_gateway_receipts: unknown[];
  invalid_gateway_receipts: Cases;
  valid_chain_receipts?: unknown[];
  invalid_chain_receipts?: Cases;
};

type SwapCorpus = {
  valid_commands: unknown[];
  canonical_command_digests: string[];
  valid_simulations: unknown[];
  canonical_simulation_digests: string[];
  valid_gateway_receipts: unknown[];
  swap_commands: unknown[];
  swap_command_digests: string[];
  invalid_swap_commands: Cases;
  swap_simulations: unknown[];
  swap_simulation_digests: string[];
  invalid_swap_simulations: Cases;
  settled_receipts: unknown[];
  invalid_settled_receipts: Cases;
};

type QuoteCorpus = {
  valid_requests: unknown[];
  invalid_requests: Cases;
  valid_results: unknown[];
  invalid_results: Cases;
};

function corpus<T>(name: string): T {
  const path = fileURLToPath(new URL(`../../contracts/execution/${name}`, import.meta.url));
  return JSON.parse(readFileSync(path, "utf8")) as T;
}

describe("execution contract corpus V2", () => {
  it("accepts every V2 command with its canonical digest and rejects invalid ones", () => {
    const v2 = corpus<ExecutionCorpus>("v2-corpus.json");

    expect(v2.valid_commands.map((value) => parseDlmmCommandV2(value).operation)).toEqual([
      "OPEN",
      "ADD",
      "REMOVE",
      "CLOSE",
    ]);
    expect(v2.valid_commands.map((value) => commandDigest(parseDlmmCommandV2(value)))).toEqual(
      v2.canonical_command_digests,
    );
    for (const invalid of v2.invalid_commands) {
      expect(DlmmCommandV2Schema.safeParse(invalid.payload).success, invalid.case).toBe(false);
    }
  });

  it("shares V2 simulations and receipts with Python", () => {
    const v2 = corpus<ExecutionCorpus>("v2-corpus.json");

    expect(
      v2.valid_simulations.map((value) => simulationDigest(parseSimulationResultV2(value))),
    ).toEqual(v2.canonical_simulation_digests);
    for (const invalid of v2.invalid_simulations) {
      expect(SimulationResultV2Schema.safeParse(invalid.payload).success, invalid.case).toBe(false);
    }
    for (const value of v2.valid_gateway_receipts) {
      expect(parseGatewayReceiptV2(value).position_address).toBe(
        "SysvarRent111111111111111111111111111111111",
      );
    }
    for (const invalid of v2.invalid_gateway_receipts) {
      expect(GatewayReceiptV2Schema.safeParse(invalid.payload).success, invalid.case).toBe(false);
    }
  });

  it("explicitly rejects every retired V1 payload as an unsupported contract version", () => {
    const v1 = corpus<ExecutionCorpus>("v1-corpus.json");
    const retired: Array<[unknown[], (value: unknown) => unknown]> = [
      [v1.valid_commands, parseDlmmCommandV2],
      [v1.valid_simulations, parseSimulationResultV2],
      [v1.valid_gateway_receipts, parseGatewayReceiptV2],
    ];

    for (const [payloads, parse] of retired) {
      for (const payload of payloads) {
        expect(() => parse(payload)).toThrow(ContractVersionError);
      }
    }
    for (const value of v1.valid_chain_receipts ?? []) {
      expect(ChainReceiptV1Schema.safeParse(value).success).toBe(true);
    }
    for (const invalid of v1.invalid_chain_receipts ?? []) {
      expect(ChainReceiptV1Schema.safeParse(invalid.payload).success, invalid.case).toBe(false);
    }
  });
});

describe("execution contract corpus V2: swap and settlement extension", () => {
  it("accepts swap commands with the shared digests and keeps every older payload valid", () => {
    const v2 = corpus<SwapCorpus>("v2-corpus.json");

    const swaps = v2.swap_commands.map((value) => parseDlmmCommandV2(value));
    expect(swaps.map((value) => [value.operation, value.in_amount, value.min_bin_id])).toEqual([
      ["SWAP", "125000000", undefined],
      ["SWAP", "1", undefined],
    ]);
    expect(swaps.map((value) => commandDigest(value))).toEqual(v2.swap_command_digests);
    expect(v2.valid_commands.map((value) => commandDigest(parseDlmmCommandV2(value)))).toEqual(
      v2.canonical_command_digests,
    );
    for (const invalid of v2.invalid_swap_commands) {
      expect(DlmmCommandV2Schema.safeParse(invalid.payload).success, invalid.case).toBe(false);
    }
  });

  it("shares swap simulations with their digests and rejects invalid swap quotes", () => {
    const v2 = corpus<SwapCorpus>("v2-corpus.json");

    const simulations = v2.swap_simulations.map((value) => parseSimulationResultV2(value));
    expect(simulations[0]?.swap_quote).toEqual({
      in_amount: "125000000",
      out_amount: "2500000000",
      min_out_amount: "2475000000",
      fee_amount: "2500000",
    });
    expect(simulations[2]?.swap_quote).toBeUndefined();
    expect(simulations.map((value) => simulationDigest(value))).toEqual(
      v2.swap_simulation_digests,
    );
    expect(
      v2.valid_simulations.map((value) => simulationDigest(parseSimulationResultV2(value))),
    ).toEqual(v2.canonical_simulation_digests);
    for (const invalid of v2.invalid_swap_simulations) {
      expect(SimulationResultV2Schema.safeParse(invalid.payload).success, invalid.case).toBe(false);
    }
  });

  it("carries the chain settlement on confirmed receipts only", () => {
    const v2 = corpus<SwapCorpus>("v2-corpus.json");

    const [swap, close, pending] = v2.settled_receipts.map((value) => parseGatewayReceiptV2(value));
    expect(swap?.settlement).toEqual({
      fee_lamports: "6200",
      priority_fee_lamports: "1200",
      delta_x: "-125000000",
      delta_y: "2491000000",
      mint_x: "SysvarS1otHashes111111111111111111111111111",
      mint_y: "So11111111111111111111111111111111111111112",
    });
    expect(close?.settlement?.delta_x).toBe("125000000");
    expect(pending?.settlement).toBeNull();
    for (const value of v2.valid_gateway_receipts) {
      expect(parseGatewayReceiptV2(value).settlement).toBeUndefined();
    }
    for (const invalid of v2.invalid_settled_receipts) {
      expect(GatewayReceiptV2Schema.safeParse(invalid.payload).success, invalid.case).toBe(false);
    }
  });
});

describe("quote contract corpus V1", () => {
  it("accepts the shared quote requests and results and rejects invalid ones", () => {
    const quotes = corpus<QuoteCorpus>("quote-v1-corpus.json");

    expect(quotes.valid_requests.map((value) => parseQuoteRequestV1(value).direction)).toEqual([
      "Y_TO_X",
      "X_TO_Y",
    ]);
    for (const invalid of quotes.invalid_requests) {
      expect(QuoteRequestV1Schema.safeParse(invalid.payload).success, invalid.case).toBe(false);
    }
    expect(() => parseQuoteRequestV1(quotes.invalid_requests[0]?.payload)).toThrow(
      ContractVersionError,
    );
    for (const value of quotes.valid_results) {
      expect(QuoteResultV1Schema.safeParse(value).success).toBe(true);
    }
    for (const invalid of quotes.invalid_results) {
      expect(QuoteResultV1Schema.safeParse(invalid.payload).success, invalid.case).toBe(false);
    }
  });
});
