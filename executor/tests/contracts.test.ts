import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  ChainReceiptV1Schema,
  DlmmCommandV1Schema,
  GatewayReceiptV1Schema,
  SimulationResultV1Schema,
} from "../src/contracts.js";
import { commandDigest, simulationDigest } from "../src/engine.js";

type Corpus = {
  valid_commands: unknown[];
  canonical_command_digests: string[];
  invalid_commands: Array<{ case: string; payload: unknown }>;
  valid_simulations: unknown[];
  canonical_simulation_digests: string[];
  invalid_simulations: Array<{ case: string; payload: unknown }>;
  valid_gateway_receipts: unknown[];
  invalid_gateway_receipts: Array<{ case: string; payload: unknown }>;
  valid_chain_receipts: unknown[];
  invalid_chain_receipts: Array<{ case: string; payload: unknown }>;
};

const corpusPath = fileURLToPath(
  new URL("../../contracts/execution/v1-corpus.json", import.meta.url),
);

describe("execution contract corpus", () => {
  it("accepts every V1 command and rejects invalid or unknown versions", () => {
    const corpus = JSON.parse(readFileSync(corpusPath, "utf8")) as Corpus;

    expect(corpus.valid_commands.map((value) => DlmmCommandV1Schema.parse(value).operation)).toEqual([
      "OPEN",
      "ADD",
      "REMOVE",
      "CLOSE",
    ]);
    expect(corpus.valid_commands.map((value) => commandDigest(DlmmCommandV1Schema.parse(value)))).toEqual(
      corpus.canonical_command_digests,
    );
    for (const invalid of corpus.invalid_commands) {
      expect(DlmmCommandV1Schema.safeParse(invalid.payload).success, invalid.case).toBe(false);
    }
  });

  it("shares simulation, submit and status payloads with Python", () => {
    const corpus = JSON.parse(readFileSync(corpusPath, "utf8")) as Corpus;

    for (const value of corpus.valid_simulations) {
      expect(SimulationResultV1Schema.safeParse(value).success).toBe(true);
    }
    expect(
      corpus.valid_simulations.map((value) =>
        simulationDigest(SimulationResultV1Schema.parse(value)),
      ),
    ).toEqual(corpus.canonical_simulation_digests);
    for (const invalid of corpus.invalid_simulations) {
      expect(SimulationResultV1Schema.safeParse(invalid.payload).success, invalid.case).toBe(false);
    }
    for (const value of corpus.valid_gateway_receipts) {
      expect(GatewayReceiptV1Schema.safeParse(value).success).toBe(true);
    }
    for (const invalid of corpus.invalid_gateway_receipts) {
      expect(GatewayReceiptV1Schema.safeParse(invalid.payload).success, invalid.case).toBe(false);
    }
    for (const value of corpus.valid_chain_receipts) {
      expect(ChainReceiptV1Schema.safeParse(value).success).toBe(true);
    }
    for (const invalid of corpus.invalid_chain_receipts) {
      expect(ChainReceiptV1Schema.safeParse(invalid.payload).success, invalid.case).toBe(false);
    }
  });
});
