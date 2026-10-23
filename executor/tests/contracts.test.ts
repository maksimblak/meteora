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
