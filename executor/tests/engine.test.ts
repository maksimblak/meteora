import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { parseDlmmCommandV1, type DlmmCommandV1 } from "../src/contracts.js";
import {
  ExecutionEngine,
  FileExecutionStateStore,
  GatewayError,
  HmacApprovalProofVerifier,
  createApprovalProof,
  type LivePromotionEvidence,
  type BuiltCommand,
  type Clock,
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

const command = (mode: "SHADOW" | "APPROVAL" | "LIVE" = "LIVE"): DlmmCommandV1 =>
  parseDlmmCommandV1({
    schema_version: 1,
    command_id: "10000000-0000-4000-8000-000000000001",
    idempotency_key: "plan-101-open-v1",
    correlation_id: "20000000-0000-4000-8000-000000000001",
    ruleset_version: "rules-2026-10-18",
    mode,
    operation: "OPEN",
    pool_address: "11111111111111111111111111111111",
    wallet_address: "11111111111111111111111111111111",
    position_address: "11111111111111111111111111111111",
    expires_at: "2026-10-18T12:10:00.000Z",
    max_slippage_bps: 50,
    strategy: "BID_ASK",
    min_bin_id: -4,
    max_bin_id: 7,
    amount_x: "1000",
    amount_y: "2000",
  });

class FakeSdk implements SdkBoundary {
  public buildCalls = 0;

  async build(value: DlmmCommandV1): Promise<BuiltCommand> {
    this.buildCalls += 1;
    return { command: value, opaqueTransactions: [new Uint8Array([1, 2, 3])] };
  }
}

class TwoTransactionSdk extends FakeSdk {
  override async build(value: DlmmCommandV1): Promise<BuiltCommand> {
    this.buildCalls += 1;
    return {
      command: value,
      opaqueTransactions: [new Uint8Array([1]), new Uint8Array([2])],
    };
  }
}

class FakeRpc implements RpcTransport {
  public submitCalls = 0;
  public statusCalls = 0;

  async simulate(): Promise<{
    computeUnits: number;
    networkFeeLamports: bigint;
    priorityFeeLamports: bigint;
    priceImpactBps: number;
    blockhash: string;
    lastValidBlockHeight: number;
    logs: string[];
  }> {
    return {
      computeUnits: 900,
      networkFeeLamports: 5000n,
      priorityFeeLamports: 0n,
      priceImpactBps: 10,
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
      schema_version: 1,
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
    const changed = parseDlmmCommandV1({ ...original, amount_x: "1001" });

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
    const second = parseDlmmCommandV1({
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
