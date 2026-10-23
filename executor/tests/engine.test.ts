import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { parseDlmmCommandV2, type DlmmCommandV2 } from "../src/contracts.js";
import {
  ExecutionEngine,
  FileExecutionStateStore,
  GatewayError,
  HmacApprovalProofVerifier,
  createApprovalProof,
  type LivePromotionEvidence,
  type BuiltCommand,
  type Clock,
  type MarketFacts,
  type RpcTransport,
  type SdkBoundary,
} from "../src/engine.js";

const now = new Date("2026-10-18T12:00:00.000Z");
const clock: Clock = { now: () => now };
const approvalSecret = "0123456789abcdef0123456789abcdef";
const temporaryDirectories: string[] = [];

afterEach(async () => {
  await Promise.all(temporaryDirectories.splice(0).map((path) => rm(path, { recursive: true })));
});

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

const POSITION = "SysvarRent111111111111111111111111111111111";

const command = (mode: "SHADOW" | "APPROVAL" | "LIVE" = "LIVE"): DlmmCommandV2 =>
  parseDlmmCommandV2({
    schema_version: 2,
    command_id: "10000000-0000-4000-8000-000000000001",
    idempotency_key: "plan-101-open-v1",
    correlation_id: "20000000-0000-4000-8000-000000000001",
    ruleset_version: "rules-2026-10-18",
    mode,
    operation: "OPEN",
    pool_address: "11111111111111111111111111111111",
    wallet_address: "11111111111111111111111111111111",
    expires_at: "2026-10-18T12:10:00.000Z",
    max_slippage_bps: 50,
    strategy: "BID_ASK",
    min_bin_id: -4,
    max_bin_id: 7,
    amount_x: "1000",
    amount_y: "2000",
    active_bin_id: 2,
    active_bin_tolerance: 3,
    token_x_decimals: 6,
    token_y_decimals: 9,
  });

const exitCommand = (): DlmmCommandV2 =>
  parseDlmmCommandV2({
    schema_version: 2,
    command_id: "10000000-0000-4000-8000-000000000009",
    idempotency_key: "position-action-close-v1",
    correlation_id: "20000000-0000-4000-8000-000000000001",
    ruleset_version: "rules-2026-10-18",
    mode: "LIVE",
    operation: "CLOSE",
    pool_address: "11111111111111111111111111111111",
    wallet_address: "11111111111111111111111111111111",
    position_address: POSITION,
    expires_at: "2026-10-18T12:10:00.000Z",
    max_slippage_bps: 50,
    min_bin_id: -4,
    max_bin_id: 7,
    remove_bps: 10_000,
  });

const facts: MarketFacts = {
  priceImpactBps: 10,
  activeBinId: 2,
  tokenXDecimals: 6,
  tokenYDecimals: 9,
};

class FakeSdk implements SdkBoundary {
  public buildCalls = 0;

  constructor(
    public market: MarketFacts = facts,
    public position: string | undefined = undefined,
  ) {}

  async build(value: DlmmCommandV2): Promise<BuiltCommand> {
    this.buildCalls += 1;
    return {
      command: value,
      opaqueTransactions: [new Uint8Array([1, 2, 3])],
      positionAddress: this.position ?? value.position_address ?? POSITION,
      market: this.market,
    };
  }
}

class TwoTransactionSdk extends FakeSdk {
  override async build(value: DlmmCommandV2): Promise<BuiltCommand> {
    this.buildCalls += 1;
    return {
      command: value,
      opaqueTransactions: [new Uint8Array([1]), new Uint8Array([2])],
      positionAddress: POSITION,
      market: facts,
    };
  }
}

class FakeRpc implements RpcTransport {
  public simulateCalls = 0;
  public submitCalls = 0;
  public statusCalls = 0;

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

  async submit(
    built?: BuiltCommand,
    recordSignature?: (signature: string) => Promise<void>,
  ): Promise<readonly string[] | string> {
    void built;
    void recordSignature;
    this.submitCalls += 1;
    return "signature-1";
  }

  async status(): Promise<{ state: "CONFIRMED"; slot: number }> {
    this.statusCalls += 1;
    return { state: "CONFIRMED", slot: 42 };
  }
}

class UncertainSubmitRpc extends FakeRpc {
  override async submit(): Promise<readonly string[] | string> {
    this.submitCalls += 1;
    throw new Error("transport failed after broadcast may have started");
  }
}

class SlowSubmitRpc extends FakeRpc {
  override async submit(): Promise<readonly string[] | string> {
    this.submitCalls += 1;
    await new Promise((resolve) => setTimeout(resolve, 20));
    return "signature-too-late";
  }
}

class PartialSubmitRpc extends FakeRpc {
  override async submit(
    _command?: BuiltCommand,
    recordSignature?: (signature: string) => Promise<void>,
  ): Promise<readonly string[] | string> {
    this.submitCalls += 1;
    await recordSignature?.("signature-1");
    throw new Error("second transaction failed before a signature was returned");
  }
}

function approvalAuthorization(
  simulation: Awaited<ReturnType<ExecutionEngine["simulate"]>>,
): {
  modeTokenVerified: true;
  approvalId: string;
  approvalApprovedAt: string;
  approvalExpiresAt: string;
  operatorId: string;
  commandDigest: string;
  simulationDigest: string;
  approvalProof: string;
} {
  const input = {
    approvalId: "40000000-0000-4000-8000-000000000001",
    approvedAt: "2026-10-18T12:00:00.000Z",
    expiresAt: "2026-10-18T12:00:30.000Z",
    operatorId: "local-operator",
    commandDigest: simulation.command_digest,
    simulationDigest: simulation.simulation_digest,
  };
  return {
    modeTokenVerified: true,
    approvalId: input.approvalId,
    approvalApprovedAt: input.approvedAt,
    approvalExpiresAt: input.expiresAt,
    operatorId: input.operatorId,
    commandDigest: input.commandDigest,
    simulationDigest: input.simulationDigest,
    approvalProof: createApprovalProof(approvalSecret, input),
  };
}

describe("execution engine", () => {
  it("maps a command through simulation and normalizes costs and range", async () => {
    const engine = new ExecutionEngine({ sdk: new FakeSdk(), rpc: new FakeRpc(), clock });

    const result = await engine.simulate(command());

    expect(result).toMatchObject({
      schema_version: 2,
      success: true,
      network_fee_lamports: "5000",
      price_impact_bps: 10,
      min_bin_id: -4,
      max_bin_id: 7,
    });
  });

  it("never submits in SHADOW or when submission is disabled", async () => {
    const rpc = new FakeRpc();
    const engine = new ExecutionEngine({ sdk: new FakeSdk(), rpc, clock });
    await engine.simulate(command("SHADOW"));

    await expect(engine.submit(command("SHADOW"), "plan-101-open-v1")).rejects.toThrow(
      GatewayError,
    );
    const disabledEngine = new ExecutionEngine({
      sdk: new FakeSdk(),
      rpc,
      clock,
      allowedMode: "LIVE",
      livePromotion,
    });
    await expect(disabledEngine.submit(command(), "plan-101-open-v1")).rejects.toThrow(
      "submission is disabled",
    );
    expect(rpc.submitCalls).toBe(0);
  });

  it("reconciles a repeated idempotency key instead of broadcasting twice", async () => {
    const rpc = new FakeRpc();
    const engine = new ExecutionEngine({
      sdk: new FakeSdk(),
      rpc,
      clock,
      submissionEnabled: true,
      allowedMode: "LIVE",
      livePromotion,
    });
    const value = command();
    await engine.simulate(value);

    const authorization = { modeTokenVerified: true };
    const first = await engine.submit(value, value.idempotency_key, authorization);
    const second = await engine.submit(value, value.idempotency_key, authorization);

    expect(first.state).toBe("SUBMITTED");
    expect(second.state).toBe("CONFIRMED");
    expect(rpc.submitCalls).toBe(1);
    expect(rpc.statusCalls).toBe(1);
  });

  it("records an uncertain submit and never broadcasts its idempotency key twice", async () => {
    const rpc = new UncertainSubmitRpc();
    const engine = new ExecutionEngine({
      sdk: new FakeSdk(),
      rpc,
      clock,
      submissionEnabled: true,
      allowedMode: "LIVE",
      livePromotion,
    });
    const value = command();
    await engine.simulate(value);

    const first = await engine.submit(value, value.idempotency_key, { modeTokenVerified: true });
    const second = await engine.submit(value, value.idempotency_key, { modeTokenVerified: true });

    expect(first).toMatchObject({
      state: "UNKNOWN",
      signature: null,
      signatures: [],
      error: { code: "SUBMIT_OUTCOME_UNKNOWN", retryable: false },
    });
    expect(second).toEqual(first);
    expect(await engine.lookup(value.command_id)).toEqual(first);
    expect(rpc.submitCalls).toBe(1);
    expect(rpc.statusCalls).toBe(0);
  });

  it("requires a current approval and a fresh simulation at the gateway boundary", async () => {
    let current = new Date(now);
    const rpc = new FakeRpc();
    const engine = new ExecutionEngine({
      sdk: new FakeSdk(),
      rpc,
      clock: { now: (): Date => current },
      submissionEnabled: true,
      allowedMode: "APPROVAL",
      simulationTtlMs: 30_000,
      approvalProofVerifier: new HmacApprovalProofVerifier(approvalSecret),
    });
    const value = command("APPROVAL");
    const simulation = await engine.simulate(value);

    await expect(
      engine.submit(value, value.idempotency_key, { modeTokenVerified: true }),
    ).rejects.toMatchObject({ code: "VALID_APPROVAL_REQUIRED" });
    current = new Date("2026-10-18T12:00:31Z");
    await expect(
      engine.submit(value, value.idempotency_key, approvalAuthorization(simulation)),
    ).rejects.toMatchObject({ code: "STALE_SIMULATION" });
    expect(rpc.submitCalls).toBe(0);
  });

  it("bounds submit timeout and records the outcome as unknown", async () => {
    const rpc = new SlowSubmitRpc();
    const engine = new ExecutionEngine({
      sdk: new FakeSdk(),
      rpc,
      clock,
      submissionEnabled: true,
      allowedMode: "LIVE",
      operationTimeoutMs: 1,
      livePromotion,
    });
    const value = command();
    await engine.simulate(value);

    const receipt = await engine.submit(value, value.idempotency_key, {
      modeTokenVerified: true,
    });

    expect(receipt.state).toBe("UNKNOWN");
    expect(receipt.attempt_count).toBe(1);
    expect(rpc.submitCalls).toBe(1);
  });

  it("coalesces concurrent submits with the same idempotency key", async () => {
    const rpc = new SlowSubmitRpc();
    const engine = new ExecutionEngine({
      sdk: new FakeSdk(),
      rpc,
      clock,
      submissionEnabled: true,
      allowedMode: "LIVE",
      livePromotion,
    });
    const value = command();
    await engine.simulate(value);

    const authorization = { modeTokenVerified: true };
    const [first, second] = await Promise.all([
      engine.submit(value, value.idempotency_key, authorization),
      engine.submit(value, value.idempotency_key, authorization),
    ]);

    expect(second).toEqual(first);
    expect(first.state).toBe("SUBMITTED");
    expect(rpc.submitCalls).toBe(1);
  });

  it("rejects a changed command payload after simulation even when command_id is reused", async () => {
    const rpc = new FakeRpc();
    const engine = new ExecutionEngine({
      sdk: new FakeSdk(),
      rpc,
      clock,
      submissionEnabled: true,
      allowedMode: "LIVE",
      livePromotion,
    });
    const original = command();
    await engine.simulate(original);
    const changed = parseDlmmCommandV2({ ...original, amount_x: "1001" });

    await expect(
      engine.submit(changed, changed.idempotency_key, { modeTokenVerified: true }),
    ).rejects.toMatchObject({ code: "SIMULATION_BINDING_MISMATCH" });
    expect(rpc.submitCalls).toBe(0);
  });

  it("keeps the prepared simulation immutable when a caller tampers with its response copy", async () => {
    const rpc = new FakeRpc();
    const engine = new ExecutionEngine({
      sdk: new FakeSdk(),
      rpc,
      clock,
      submissionEnabled: true,
      allowedMode: "LIVE",
      livePromotion,
    });
    const value = command();
    const response = await engine.simulate(value);
    response.price_impact_bps = 9_999;
    response.simulation_digest = "0".repeat(64);

    const receipt = await engine.submit(value, value.idempotency_key, {
      modeTokenVerified: true,
    });

    expect(receipt.state).toBe("SUBMITTED");
    expect(rpc.submitCalls).toBe(1);
  });

  it("rejects forged and non-finite approval timestamps", async () => {
    const rpc = new FakeRpc();
    const engine = new ExecutionEngine({
      sdk: new FakeSdk(),
      rpc,
      clock,
      submissionEnabled: true,
      allowedMode: "APPROVAL",
      approvalProofVerifier: new HmacApprovalProofVerifier(approvalSecret),
    });
    const value = command("APPROVAL");
    const simulation = await engine.simulate(value);
    const valid = approvalAuthorization(simulation);

    await expect(
      engine.submit(value, value.idempotency_key, { ...valid, approvalProof: "0".repeat(64) }),
    ).rejects.toMatchObject({ code: "VALID_APPROVAL_REQUIRED" });
    await expect(
      engine.submit(value, value.idempotency_key, {
        ...valid,
        approvalExpiresAt: "NaN",
      }),
    ).rejects.toMatchObject({ code: "VALID_APPROVAL_REQUIRED" });
    expect(rpc.submitCalls).toBe(0);
  });

  it("keeps LIVE closed until all promotion evidence is explicit", async () => {
    const rpc = new FakeRpc();
    const engine = new ExecutionEngine({
      sdk: new FakeSdk(),
      rpc,
      clock,
      submissionEnabled: true,
      allowedMode: "LIVE",
    });
    const value = command();
    await engine.simulate(value);

    await expect(
      engine.submit(value, value.idempotency_key, { modeTokenVerified: true }),
    ).rejects.toMatchObject({ code: "LIVE_PROMOTION_REQUIRED" });
    expect(rpc.submitCalls).toBe(0);
  });

  it("keeps LIVE closed below ten reconciled approval cycles or without the checklist", async () => {
    const rpc = new FakeRpc();
    const engine = new ExecutionEngine({
      sdk: new FakeSdk(),
      rpc,
      clock,
      submissionEnabled: true,
      allowedMode: "LIVE",
      livePromotion: { ...livePromotion, approvalCycles: 9, checklistCompleted: false },
    });
    const value = command();
    await engine.simulate(value);

    await expect(
      engine.submit(value, value.idempotency_key, { modeTokenVerified: true }),
    ).rejects.toMatchObject({ code: "LIVE_PROMOTION_REQUIRED" });
    expect(rpc.submitCalls).toBe(0);
  });

  it("claims a signed approval once across different command bindings and restarts", async () => {
    const directory = await mkdtemp(join(tmpdir(), "executor-approval-"));
    temporaryDirectories.push(directory);
    const statePath = join(directory, "execution-state.json");
    const rpc = new FakeRpc();
    const firstEngine = new ExecutionEngine({
      sdk: new FakeSdk(),
      rpc,
      store: new FileExecutionStateStore(statePath),
      clock,
      submissionEnabled: true,
      allowedMode: "APPROVAL",
      approvalProofVerifier: new HmacApprovalProofVerifier(approvalSecret),
    });
    const first = command("APPROVAL");
    const second = parseDlmmCommandV2({
      ...first,
      command_id: "10000000-0000-4000-8000-000000000002",
      idempotency_key: "plan-102-open-v1",
    });
    const firstSimulation = await firstEngine.simulate(first);

    await firstEngine.submit(
      first,
      first.idempotency_key,
      approvalAuthorization(firstSimulation),
    );
    const restarted = new ExecutionEngine({
      sdk: new FakeSdk(),
      rpc,
      store: new FileExecutionStateStore(statePath),
      clock,
      submissionEnabled: true,
      allowedMode: "APPROVAL",
      approvalProofVerifier: new HmacApprovalProofVerifier(approvalSecret),
    });
    const secondSimulation = await restarted.simulate(second);
    await expect(
      restarted.submit(
        second,
        second.idempotency_key,
        approvalAuthorization(secondSimulation),
      ),
    ).rejects.toMatchObject({ code: "APPROVAL_ALREADY_USED" });
    expect(rpc.submitCalls).toBe(1);
  });

  it("persists idempotency across an executor restart", async () => {
    const directory = await mkdtemp(join(tmpdir(), "executor-state-"));
    temporaryDirectories.push(directory);
    const statePath = join(directory, "execution-state.json");
    const rpc = new FakeRpc();
    const value = command();
    const firstEngine = new ExecutionEngine({
      sdk: new FakeSdk(),
      rpc,
      store: new FileExecutionStateStore(statePath),
      clock,
      submissionEnabled: true,
      allowedMode: "LIVE",
      livePromotion,
    });
    await firstEngine.simulate(value);
    await firstEngine.submit(value, value.idempotency_key, { modeTokenVerified: true });

    const restarted = new ExecutionEngine({
      sdk: new FakeSdk(),
      rpc,
      store: new FileExecutionStateStore(statePath),
      clock,
      submissionEnabled: true,
      allowedMode: "LIVE",
      livePromotion,
    });
    const receipt = await restarted.submit(value, value.idempotency_key, {
      modeTokenVerified: true,
    });

    expect(receipt.state).toBe("CONFIRMED");
    expect(rpc.submitCalls).toBe(1);
  });

  it("persists partial multi-transaction signatures and never re-broadcasts after restart", async () => {
    const directory = await mkdtemp(join(tmpdir(), "executor-partial-"));
    temporaryDirectories.push(directory);
    const statePath = join(directory, "execution-state.json");
    const rpc = new PartialSubmitRpc();
    const value = command();
    const firstEngine = new ExecutionEngine({
      sdk: new TwoTransactionSdk(),
      rpc,
      store: new FileExecutionStateStore(statePath),
      clock,
      submissionEnabled: true,
      allowedMode: "LIVE",
      livePromotion,
    });
    await firstEngine.simulate(value);
    const partial = await firstEngine.submit(value, value.idempotency_key, {
      modeTokenVerified: true,
    });

    expect(partial).toMatchObject({
      state: "UNKNOWN",
      signature: "signature-1",
      signatures: ["signature-1"],
      transaction_count: 2,
    });
    const restarted = new ExecutionEngine({
      sdk: new TwoTransactionSdk(),
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

    expect(reconciled.state).toBe("UNKNOWN");
    expect(reconciled.signatures).toEqual(["signature-1"]);
    expect(rpc.submitCalls).toBe(1);
    expect(rpc.statusCalls).toBe(1);
  });
});

describe("execution engine market binding (V2)", () => {
  it("returns the executor-assigned position and observed market facts", async () => {
    const assigned = "SysvarC1ock11111111111111111111111111111111";
    const engine = new ExecutionEngine({
      sdk: new FakeSdk(facts, assigned),
      rpc: new FakeRpc(),
      clock,
      submissionEnabled: true,
      allowedMode: "LIVE",
      livePromotion,
    });
    const value = command();

    const simulation = await engine.simulate(value);
    const receipt = await engine.submit(value, value.idempotency_key, { modeTokenVerified: true });

    expect(simulation).toMatchObject({
      schema_version: 2,
      success: true,
      price_impact_bps: 10,
      position_address: assigned,
      active_bin_id: 2,
      token_x_decimals: 6,
      token_y_decimals: 9,
    });
    expect(receipt).toMatchObject({
      schema_version: 2,
      state: "SUBMITTED",
      position_address: assigned,
    });
  });

  it("accepts the anchor tolerance and rejects a pool that moved beyond it", async () => {
    const rpc = new FakeRpc();
    const within = new ExecutionEngine({
      sdk: new FakeSdk({ ...facts, activeBinId: 2 + 3 }),
      rpc,
      clock,
    });
    const beyond = new ExecutionEngine({
      sdk: new FakeSdk({ ...facts, activeBinId: 2 - 4 }),
      rpc,
      clock,
    });

    const accepted = await within.simulate(command());
    const rejected = await beyond.simulate(command());

    expect(accepted.success).toBe(true);
    expect(rejected).toMatchObject({
      success: false,
      active_bin_id: -2,
      position_address: null,
      errors: [{ code: "ACTIVE_BIN_DRIFT", retryable: false }],
    });
    expect(rpc.simulateCalls).toBe(1);
  });

  it("rejects mint decimals that differ from the ones the amounts were computed with", async () => {
    const engine = new ExecutionEngine({
      sdk: new FakeSdk({ ...facts, tokenXDecimals: 9 }),
      rpc: new FakeRpc(),
      clock,
    });

    const result = await engine.simulate(command());

    expect(result).toMatchObject({
      success: false,
      token_x_decimals: 9,
      errors: [{ code: "TOKEN_DECIMALS_MISMATCH" }],
    });
  });

  it("fails a quote above the policy limit before simulating any transaction", async () => {
    const rpc = new FakeRpc();
    const engine = new ExecutionEngine({
      sdk: new FakeSdk({ ...facts, priceImpactBps: 51 }),
      rpc,
      clock,
      submissionEnabled: true,
      allowedMode: "LIVE",
      livePromotion,
    });
    const value = command();

    const result = await engine.simulate(value);

    expect(result).toMatchObject({
      success: false,
      price_impact_bps: 51,
      errors: [{ code: "SLIPPAGE_BREACH" }],
    });
    expect(rpc.simulateCalls).toBe(0);
    await expect(
      engine.submit(value, value.idempotency_key, { modeTokenVerified: true }),
    ).rejects.toMatchObject({ code: "FRESH_SIMULATION_REQUIRED" });
    expect(rpc.submitCalls).toBe(0);
  });

  it("fails closed when the SDK quote did not measure a finite price impact", async () => {
    const engine = new ExecutionEngine({
      sdk: new FakeSdk({ ...facts, priceImpactBps: Number.NaN }),
      rpc: new FakeRpc(),
      clock,
    });

    const result = await engine.simulate(command());

    expect(result).toMatchObject({
      success: false,
      price_impact_bps: null,
      errors: [{ code: "PRICE_IMPACT_UNAVAILABLE" }],
    });
  });

  it("never lets an exit act on a position other than the command's", async () => {
    const engine = new ExecutionEngine({
      sdk: new FakeSdk(facts, "SysvarC1ock11111111111111111111111111111111"),
      rpc: new FakeRpc(),
      clock,
    });

    const result = await engine.simulate(exitCommand());

    expect(result).toMatchObject({
      success: false,
      errors: [{ code: "POSITION_BINDING_MISMATCH" }],
    });
  });
});

describe("exits are not gated by the price impact of a swap they never make (D08)", () => {
  it("lets an exit through a thin pool and reports its quoted impact", async () => {
    const rpc = new FakeRpc();
    const engine = new ExecutionEngine({
      sdk: new FakeSdk({ ...facts, priceImpactBps: 9_000 }),
      rpc,
      clock,
      submissionEnabled: true,
      allowedMode: "LIVE",
      livePromotion,
    });
    const value = exitCommand();

    const simulation = await engine.simulate(value);
    const receipt = await engine.submit(value, value.idempotency_key, { modeTokenVerified: true });

    expect(simulation).toMatchObject({ success: true, price_impact_bps: 9_000, errors: [] });
    expect(receipt).toMatchObject({ state: "SUBMITTED", position_address: POSITION });
    expect(rpc.submitCalls).toBe(1);
  });

  it("lets an exit through when its swap cannot be quoted", async () => {
    const engine = new ExecutionEngine({
      sdk: new FakeSdk({ ...facts, priceImpactBps: null }),
      rpc: new FakeRpc(),
      clock,
    });

    const simulation = await engine.simulate(exitCommand());

    expect(simulation).toMatchObject({ success: true, price_impact_bps: null });
  });
});
