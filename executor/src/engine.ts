import { createHash, createHmac, timingSafeEqual } from "node:crypto";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { dirname } from "node:path";

import type {
  ChainReceiptV1,
  DlmmCommandV1,
  GatewayReceiptV1,
  SimulationResultV1,
} from "./contracts.js";

export interface Clock {
  now(): Date;
}

export interface BuiltCommand {
  command: DlmmCommandV1;
  opaqueTransactions: readonly unknown[];
  opaqueSigners?: readonly unknown[];
  priceImpactBps?: number;
}

export interface SdkBoundary {
  build(command: DlmmCommandV1): Promise<BuiltCommand>;
}

export interface SimulationTransportResult {
  computeUnits: number;
  networkFeeLamports: bigint;
  priorityFeeLamports: bigint;
  priceImpactBps: number;
  blockhash: string;
  lastValidBlockHeight: number;
  logs: string[];
}

export interface RpcStatus {
  state: "PENDING" | "CONFIRMED" | "FAILED" | "UNKNOWN";
  slot: number | null;
  error?: string;
}

export type SignatureRecorder = (signature: string) => Promise<void>;

export interface RpcTransport {
  simulate(command: BuiltCommand): Promise<SimulationTransportResult>;
  submit(
    command: BuiltCommand,
    recordSignature?: SignatureRecorder,
  ): Promise<readonly string[] | string>;
  status(signature: string): Promise<RpcStatus>;
}

export interface SubmitAuthorization {
  modeTokenVerified: boolean;
  approvalId?: string;
  approvalApprovedAt?: string;
  approvalExpiresAt?: string;
  operatorId?: string;
  commandDigest?: string;
  simulationDigest?: string;
  approvalProof?: string;
}

export interface ApprovalProofInput {
  approvalId: string;
  approvedAt: string;
  expiresAt: string;
  operatorId: string;
  commandDigest: string;
  simulationDigest: string;
}

export interface ApprovalProofVerifier {
  verify(input: ApprovalProofInput, proof: string): boolean;
}

export class HmacApprovalProofVerifier implements ApprovalProofVerifier {
  readonly #secret: Buffer;

  constructor(secret: string | Buffer) {
    this.#secret = Buffer.from(secret);
    if (this.#secret.length < 32) {
      throw new Error("approval proof secret must contain at least 32 bytes");
    }
  }

  verify(input: ApprovalProofInput, proof: string): boolean {
    if (!/^[a-f0-9]{64}$/.test(proof)) {
      return false;
    }
    const expected = Buffer.from(createApprovalProof(this.#secret, input), "hex");
    const actual = Buffer.from(proof, "hex");
    return actual.length === expected.length && timingSafeEqual(actual, expected);
  }
}

export function createApprovalProof(
  secret: string | Buffer,
  input: ApprovalProofInput,
): string {
  return createHmac("sha256", secret).update(approvalProofMessage(input)).digest("hex");
}

export interface LivePromotionEvidence {
  promotionId: string;
  rulesetVersion: string;
  approvalCycles: number;
  receiptsReconciled: boolean;
  riskInvariantsPreserved: boolean;
  dedicatedRpcReady: boolean;
  walletReady: boolean;
  checklistCompleted: boolean;
  operatorConfirmationId: string;
  promotedAt: string;
  expiresAt: string;
}

export type TradingMode = "SHADOW" | "APPROVAL" | "LIVE";

export interface PersistedSubmission {
  commandId: string;
  commandDigest: string;
  simulationDigest: string;
  idempotencyKey: string;
  approvalId?: string;
  receipt: GatewayReceiptV1;
}

export type ReserveSubmissionResult =
  | { created: true; state: PersistedSubmission }
  | {
      created: false;
      state?: PersistedSubmission;
      conflict?: "IDEMPOTENCY" | "COMMAND" | "APPROVAL";
    };

export interface ExecutionStateStore {
  getByKey(idempotencyKey: string): Promise<PersistedSubmission | undefined>;
  getByCommand(commandId: string): Promise<PersistedSubmission | undefined>;
  reserve(state: PersistedSubmission): Promise<ReserveSubmissionResult>;
  update(state: PersistedSubmission): Promise<void>;
}

export class InMemoryExecutionStateStore implements ExecutionStateStore {
  readonly #byKey = new Map<string, PersistedSubmission>();
  readonly #keyByCommand = new Map<string, string>();
  readonly #approvalBindings = new Map<string, string>();

  async getByKey(idempotencyKey: string): Promise<PersistedSubmission | undefined> {
    return cloneState(this.#byKey.get(idempotencyKey));
  }

  async getByCommand(commandId: string): Promise<PersistedSubmission | undefined> {
    const key = this.#keyByCommand.get(commandId);
    return key === undefined ? undefined : cloneState(this.#byKey.get(key));
  }

  async reserve(state: PersistedSubmission): Promise<ReserveSubmissionResult> {
    const existing = this.#byKey.get(state.idempotencyKey);
    if (existing !== undefined) {
      const sameBinding =
        existing.commandId === state.commandId &&
        existing.commandDigest === state.commandDigest &&
        existing.simulationDigest === state.simulationDigest;
      return sameBinding
        ? { created: false, state: cloneState(existing) as PersistedSubmission }
        : { created: false, conflict: "IDEMPOTENCY" };
    }
    const commandKey = this.#keyByCommand.get(state.commandId);
    if (commandKey !== undefined && commandKey !== state.idempotencyKey) {
      return { created: false, conflict: "COMMAND" };
    }
    if (state.approvalId !== undefined) {
      const binding = `${state.commandDigest}:${state.simulationDigest}`;
      const existingBinding = this.#approvalBindings.get(state.approvalId);
      if (existingBinding !== undefined && existingBinding !== binding) {
        return { created: false, conflict: "APPROVAL" };
      }
      this.#approvalBindings.set(state.approvalId, binding);
    }
    this.#byKey.set(state.idempotencyKey, cloneState(state) as PersistedSubmission);
    this.#keyByCommand.set(state.commandId, state.idempotencyKey);
    return { created: true, state: cloneState(state) as PersistedSubmission };
  }

  async update(state: PersistedSubmission): Promise<void> {
    const existing = this.#byKey.get(state.idempotencyKey);
    if (existing === undefined || existing.commandId !== state.commandId) {
      throw new Error("submission intent was not reserved");
    }
    this.#byKey.set(state.idempotencyKey, cloneState(state) as PersistedSubmission);
  }
}

interface StateDocument {
  schemaVersion: 1;
  submissions: PersistedSubmission[];
}

export class FileExecutionStateStore implements ExecutionStateStore {
  readonly #path: string;
  #tail: Promise<void> = Promise.resolve();

  constructor(path: string) {
    if (path.trim().length === 0) {
      throw new Error("execution state path is required");
    }
    this.#path = path;
  }

  async getByKey(idempotencyKey: string): Promise<PersistedSubmission | undefined> {
    return this.#locked(async () => {
      const document = await this.#read();
      return cloneState(document.submissions.find((item) => item.idempotencyKey === idempotencyKey));
    });
  }

  async getByCommand(commandId: string): Promise<PersistedSubmission | undefined> {
    return this.#locked(async () => {
      const document = await this.#read();
      return cloneState(document.submissions.find((item) => item.commandId === commandId));
    });
  }

  async reserve(state: PersistedSubmission): Promise<ReserveSubmissionResult> {
    return this.#locked(async () => {
      const document = await this.#read();
      const existing = document.submissions.find(
        (item) => item.idempotencyKey === state.idempotencyKey,
      );
      if (existing !== undefined) {
        const sameBinding =
          existing.commandId === state.commandId &&
          existing.commandDigest === state.commandDigest &&
          existing.simulationDigest === state.simulationDigest;
        return sameBinding
          ? { created: false, state: cloneState(existing) as PersistedSubmission }
          : { created: false, conflict: "IDEMPOTENCY" };
      }
      if (
        document.submissions.some(
          (item) =>
            item.commandId === state.commandId && item.idempotencyKey !== state.idempotencyKey,
        )
      ) {
        return { created: false, conflict: "COMMAND" };
      }
      if (
        state.approvalId !== undefined &&
        document.submissions.some(
          (item) =>
            item.approvalId === state.approvalId &&
            (item.commandDigest !== state.commandDigest ||
              item.simulationDigest !== state.simulationDigest),
        )
      ) {
        return { created: false, conflict: "APPROVAL" };
      }
      document.submissions.push(cloneState(state) as PersistedSubmission);
      await this.#write(document);
      return { created: true, state: cloneState(state) as PersistedSubmission };
    });
  }

  async update(state: PersistedSubmission): Promise<void> {
    await this.#locked(async () => {
      const document = await this.#read();
      const index = document.submissions.findIndex(
        (item) => item.idempotencyKey === state.idempotencyKey,
      );
      if (index < 0 || document.submissions[index]?.commandId !== state.commandId) {
        throw new Error("submission intent was not reserved");
      }
      document.submissions[index] = cloneState(state) as PersistedSubmission;
      await this.#write(document);
    });
  }

  async #read(): Promise<StateDocument> {
    try {
      const value = JSON.parse(await readFile(this.#path, "utf8")) as StateDocument;
      if (value.schemaVersion !== 1 || !Array.isArray(value.submissions)) {
        throw new Error("invalid durable execution state");
      }
      return value;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") {
        return { schemaVersion: 1, submissions: [] };
      }
      throw error;
    }
  }

  async #write(document: StateDocument): Promise<void> {
    await mkdir(dirname(this.#path), { recursive: true });
    const temporary = `${this.#path}.${process.pid}.${Date.now()}.tmp`;
    await writeFile(temporary, JSON.stringify(document), { encoding: "utf8", mode: 0o600 });
    await rename(temporary, this.#path);
  }

  async #locked<T>(operation: () => Promise<T>): Promise<T> {
    const previous = this.#tail;
    let release: (() => void) | undefined;
    this.#tail = new Promise<void>((resolve) => {
      release = resolve;
    });
    await previous;
    try {
      return await operation();
    } finally {
      release?.();
    }
  }
}

export interface ExecutionEngineOptions {
  sdk: SdkBoundary;
  rpc: RpcTransport;
  store?: ExecutionStateStore;
  clock?: Clock;
  submissionEnabled?: boolean;
  allowedMode?: TradingMode;
  simulationTtlMs?: number;
  operationTimeoutMs?: number;
  approvalProofVerifier?: ApprovalProofVerifier;
  livePromotion?: LivePromotionEvidence;
}

export class GatewayError extends Error {
  constructor(
    public readonly code: string,
    message: string,
    public readonly statusCode = 409,
  ) {
    super(message);
    this.name = "GatewayError";
  }
}

const systemClock: Clock = { now: () => new Date() };

interface PreparedCommand {
  built: BuiltCommand;
  commandDigest: string;
  simulation: SimulationResultV1;
}

export class ExecutionEngine {
  readonly #sdk: SdkBoundary;
  readonly #rpc: RpcTransport;
  readonly #store: ExecutionStateStore;
  readonly #clock: Clock;
  readonly #submissionEnabled: boolean;
  readonly #allowedMode: TradingMode;
  readonly #simulationTtlMs: number;
  readonly #operationTimeoutMs: number;
  readonly #approvalProofVerifier: ApprovalProofVerifier | undefined;
  readonly #livePromotion: LivePromotionEvidence | undefined;
  readonly #prepared = new Map<string, PreparedCommand>();
  readonly #submissionsByKey = new Map<
    string,
    { commandDigest: string; promise: Promise<GatewayReceiptV1> }
  >();

  constructor(options: ExecutionEngineOptions) {
    this.#sdk = options.sdk;
    this.#rpc = options.rpc;
    this.#store = options.store ?? new InMemoryExecutionStateStore();
    this.#clock = options.clock ?? systemClock;
    this.#submissionEnabled = options.submissionEnabled ?? false;
    this.#allowedMode = options.allowedMode ?? "SHADOW";
    this.#simulationTtlMs = options.simulationTtlMs ?? 120_000;
    this.#operationTimeoutMs = options.operationTimeoutMs ?? 15_000;
    this.#approvalProofVerifier = options.approvalProofVerifier;
    this.#livePromotion = options.livePromotion;
    if (this.#simulationTtlMs <= 0 || !Number.isFinite(this.#simulationTtlMs)) {
      throw new Error("simulationTtlMs must be positive");
    }
    if (this.#operationTimeoutMs <= 0 || !Number.isFinite(this.#operationTimeoutMs)) {
      throw new Error("operationTimeoutMs must be positive");
    }
  }

  async simulate(command: DlmmCommandV1): Promise<SimulationResultV1> {
    this.#assertNotExpired(command);
    const immutableCommand = deepFreeze(structuredClone(command));
    const digest = commandDigest(immutableCommand);
    try {
      const sdkBuilt = await this.#withTimeout(this.#sdk.build(immutableCommand));
      if (commandDigest(sdkBuilt.command) !== digest || sdkBuilt.opaqueTransactions.length === 0) {
        throw new GatewayError("BUILD_BINDING_MISMATCH", "SDK build does not match the command");
      }
      const built: BuiltCommand = {
        ...sdkBuilt,
        command: immutableCommand,
        opaqueTransactions: Object.freeze([...sdkBuilt.opaqueTransactions]),
        ...(sdkBuilt.opaqueSigners === undefined
          ? {}
          : { opaqueSigners: Object.freeze([...sdkBuilt.opaqueSigners]) }),
      };
      const result = await this.#withTimeout(this.#rpc.simulate(built));
      this.#assertMeasuredSimulation(result);
      const success = result.priceImpactBps <= immutableCommand.max_slippage_bps;
      const normalized = deepFreeze(finalizeSimulation({
        schema_version: 1,
        command_id: immutableCommand.command_id,
        command_digest: digest,
        success,
        simulated_at: this.#clock.now().toISOString(),
        blockhash: result.blockhash,
        last_valid_block_height: result.lastValidBlockHeight,
        compute_units: result.computeUnits,
        network_fee_lamports: result.networkFeeLamports.toString(),
        priority_fee_lamports: result.priorityFeeLamports.toString(),
        price_impact_bps: result.priceImpactBps,
        min_bin_id: immutableCommand.min_bin_id,
        max_bin_id: immutableCommand.max_bin_id,
        errors: success
          ? []
          : [
              {
                code: "SLIPPAGE_BREACH",
                message: "simulation exceeds the command slippage limit",
                retryable: false,
              },
            ],
      }));
      this.#prepared.set(immutableCommand.command_id, {
        built,
        commandDigest: digest,
        simulation: normalized,
      });
      return structuredClone(normalized);
    } catch (error) {
      this.#prepared.delete(immutableCommand.command_id);
      return deepFreeze(finalizeSimulation({
        schema_version: 1,
        command_id: immutableCommand.command_id,
        command_digest: digest,
        success: false,
        simulated_at: this.#clock.now().toISOString(),
        blockhash: null,
        last_valid_block_height: null,
        compute_units: 0,
        network_fee_lamports: "0",
        priority_fee_lamports: "0",
        price_impact_bps: null,
        min_bin_id: immutableCommand.min_bin_id,
        max_bin_id: immutableCommand.max_bin_id,
        errors: [this.#normalizeError(error, "SIMULATION_FAILED")],
      }));
    }
  }

  async submit(
    command: DlmmCommandV1,
    idempotencyKey: string,
    authorization: SubmitAuthorization = { modeTokenVerified: false },
  ): Promise<GatewayReceiptV1> {
    if (idempotencyKey.length === 0 || idempotencyKey !== command.idempotency_key) {
      throw new GatewayError("IDEMPOTENCY_MISMATCH", "idempotency key does not match command");
    }
    const digest = commandDigest(command);
    const existing = await this.#store.getByKey(idempotencyKey);
    if (existing !== undefined) {
      this.#assertStoredBinding(existing, command.command_id, digest);
      return this.#reconcileReceipt(existing);
    }

    if (command.mode === "SHADOW" || this.#allowedMode === "SHADOW") {
      throw new GatewayError("SHADOW_NO_SUBMIT", "SHADOW mode never submits");
    }
    if (!this.#submissionEnabled) {
      throw new GatewayError("SUBMISSION_DISABLED", "submission is disabled");
    }
    if (command.mode !== this.#allowedMode) {
      throw new GatewayError("MODE_NOT_AUTHORIZED", "command mode is not authorized");
    }
    if (!authorization.modeTokenVerified) {
      throw new GatewayError("MODE_TOKEN_REQUIRED", "mode authorization is required", 401);
    }
    this.#assertNotExpired(command);

    const prepared = this.#prepared.get(command.command_id);
    if (prepared === undefined || !prepared.simulation.success) {
      throw new GatewayError("FRESH_SIMULATION_REQUIRED", "a successful simulation is required");
    }
    this.#assertPreparedBinding(prepared, command, digest);
    this.#assertFreshSimulation(prepared.simulation);
    if (
      prepared.simulation.price_impact_bps === null ||
      prepared.simulation.price_impact_bps > command.max_slippage_bps
    ) {
      throw new GatewayError("SLIPPAGE_BREACH", "simulation exceeds the command slippage limit");
    }
    if (command.mode === "APPROVAL") {
      this.#assertApproval(command, prepared.simulation, authorization);
    } else if (command.mode === "LIVE") {
      this.#assertLivePromotion(command);
    }

    const active = this.#submissionsByKey.get(idempotencyKey);
    if (active !== undefined) {
      if (active.commandDigest !== digest) {
        throw new GatewayError("IDEMPOTENCY_CONFLICT", "idempotency key belongs to another command");
      }
      return active.promise;
    }

    const submission = this.#broadcast(
      command,
      digest,
      prepared.simulation,
      idempotencyKey,
      prepared.built,
      authorization.approvalId,
    );
    this.#submissionsByKey.set(idempotencyKey, { commandDigest: digest, promise: submission });
    try {
      return await submission;
    } finally {
      const current = this.#submissionsByKey.get(idempotencyKey);
      if (current?.promise === submission) {
        this.#submissionsByKey.delete(idempotencyKey);
      }
    }
  }

  async status(signature: string): Promise<ChainReceiptV1> {
    if (signature.length === 0) {
      throw new GatewayError("INVALID_SIGNATURE", "signature is required", 400);
    }
    const status = await this.#withTimeout(this.#rpc.status(signature));
    return {
      schema_version: 1,
      signature,
      state: status.state,
      checked_at: this.#clock.now().toISOString(),
      slot: status.slot,
      error: status.error ?? null,
    };
  }

  async lookup(commandId: string): Promise<GatewayReceiptV1 | undefined> {
    return (await this.#store.getByCommand(commandId))?.receipt;
  }

  async get(commandId: string): Promise<GatewayReceiptV1 | undefined> {
    const state = await this.#store.getByCommand(commandId);
    return state === undefined ? undefined : this.#reconcileReceipt(state);
  }

  async #reconcileReceipt(state: PersistedSubmission): Promise<GatewayReceiptV1> {
    const receipt = state.receipt;
    if (receipt.state === "CONFIRMED" || receipt.state === "FAILED") {
      return receipt;
    }
    if (receipt.signatures.length === 0) {
      return receipt;
    }
    const statuses = await Promise.all(
      receipt.signatures.map(async (signature) => {
        try {
          return await this.#withTimeout(this.#rpc.status(signature));
        } catch {
          return { state: "UNKNOWN" as const, slot: null };
        }
      }),
    );
    let next: GatewayReceiptV1["state"];
    if (statuses.some((item) => item.state === "FAILED")) {
      next = "FAILED";
    } else if (
      receipt.signatures.length === receipt.transaction_count &&
      statuses.every((item) => item.state === "CONFIRMED")
    ) {
      next = "CONFIRMED";
    } else if (
      receipt.signatures.length < receipt.transaction_count ||
      statuses.some((item) => item.state === "UNKNOWN")
    ) {
      next = "UNKNOWN";
    } else {
      next = "SUBMITTED";
    }
    const updated: PersistedSubmission = {
      ...state,
      receipt: { ...receipt, state: next },
    };
    await this.#store.update(updated);
    return updated.receipt;
  }

  async #broadcast(
    command: DlmmCommandV1,
    digest: string,
    simulation: SimulationResultV1,
    idempotencyKey: string,
    built: BuiltCommand,
    approvalId: string | undefined,
  ): Promise<GatewayReceiptV1> {
    const transactionCount = built.opaqueTransactions.length;
    const submittedAt = this.#clock.now().toISOString();
    let state: PersistedSubmission = {
      commandId: command.command_id,
      commandDigest: digest,
      simulationDigest: simulation.simulation_digest,
      idempotencyKey,
      ...(approvalId === undefined ? {} : { approvalId }),
      receipt: {
        schema_version: 1,
        command_id: command.command_id,
        command_digest: digest,
        simulation_digest: simulation.simulation_digest,
        idempotency_key: idempotencyKey,
        state: "UNKNOWN",
        signature: null,
        signatures: [],
        transaction_count: transactionCount,
        submitted_at: submittedAt,
        attempt_count: 1,
        error: {
          code: "SUBMISSION_INTENT_RECORDED",
          message: "durable submission intent awaits network reconciliation",
          retryable: false,
        },
      },
    };
    const reserved = await this.#store.reserve(state);
    if (!reserved.created) {
      if (reserved.state !== undefined) {
        return this.#reconcileReceipt(reserved.state);
      }
      const code =
        reserved.conflict === "APPROVAL" ? "APPROVAL_ALREADY_USED" : "IDEMPOTENCY_CONFLICT";
      throw new GatewayError(code, "durable execution binding already belongs to another request");
    }

    const signatures: string[] = [];
    const recordSignature = async (signature: string): Promise<void> => {
      if (signature.trim().length === 0) {
        throw new GatewayError("EMPTY_RECEIPT", "RPC returned an empty transaction signature");
      }
      if (!signatures.includes(signature)) {
        signatures.push(signature);
      }
      if (signatures.length > transactionCount) {
        throw new GatewayError("RECEIPT_COUNT_MISMATCH", "RPC returned too many signatures");
      }
      state = {
        ...state,
        receipt: {
          ...state.receipt,
          signature: signatures.at(-1) ?? null,
          signatures: [...signatures],
          error: {
            code: "PARTIAL_BROADCAST",
            message: "one or more transactions require reconciliation",
            retryable: false,
          },
        },
      };
      await this.#store.update(state);
    };

    try {
      const submitted = await this.#withTimeout(this.#rpc.submit(built, recordSignature));
      const returned = typeof submitted === "string" ? [submitted] : [...submitted];
      for (const signature of returned) {
        await recordSignature(signature);
      }
      if (signatures.length !== transactionCount) {
        throw new GatewayError(
          "RECEIPT_COUNT_MISMATCH",
          "RPC did not return every transaction signature",
        );
      }
      state = {
        ...state,
        receipt: {
          ...state.receipt,
          state: "SUBMITTED",
          signature: signatures.at(-1) ?? null,
          signatures: [...signatures],
          error: null,
        },
      };
    } catch {
      state = {
        ...state,
        receipt: {
          ...state.receipt,
          state: "UNKNOWN",
          signature: signatures.at(-1) ?? null,
          signatures: [...signatures],
          error: {
            code: "SUBMIT_OUTCOME_UNKNOWN",
            message: "submit outcome requires reconciliation",
            retryable: false,
          },
        },
      };
    }
    await this.#store.update(state);
    return state.receipt;
  }

  #assertNotExpired(command: DlmmCommandV1): void {
    const expiry = Date.parse(command.expires_at);
    if (!Number.isFinite(expiry) || expiry <= this.#clock.now().getTime()) {
      throw new GatewayError("COMMAND_EXPIRED", "command has expired");
    }
  }

  #assertPreparedBinding(
    prepared: PreparedCommand,
    command: DlmmCommandV1,
    digest: string,
  ): void {
    if (
      prepared.commandDigest !== digest ||
      prepared.simulation.command_digest !== digest ||
      prepared.simulation.command_id !== command.command_id ||
      prepared.simulation.simulation_digest !== simulationDigest(prepared.simulation) ||
      commandDigest(prepared.built.command) !== digest
    ) {
      throw new GatewayError(
        "SIMULATION_BINDING_MISMATCH",
        "simulation and built transaction do not match the submitted command",
      );
    }
  }

  #assertStoredBinding(state: PersistedSubmission, commandId: string, digest: string): void {
    if (state.commandId !== commandId || state.commandDigest !== digest) {
      throw new GatewayError("IDEMPOTENCY_CONFLICT", "idempotency key belongs to another command");
    }
  }

  #assertFreshSimulation(simulation: SimulationResultV1): void {
    const simulatedAt = Date.parse(simulation.simulated_at);
    const age = this.#clock.now().getTime() - simulatedAt;
    if (!Number.isFinite(simulatedAt) || age < 0 || age >= this.#simulationTtlMs) {
      throw new GatewayError("STALE_SIMULATION", "simulation is stale");
    }
  }

  #assertApproval(
    command: DlmmCommandV1,
    simulation: SimulationResultV1,
    authorization: SubmitAuthorization,
  ): void {
    const {
      approvalId,
      approvalApprovedAt,
      approvalExpiresAt,
      operatorId,
      commandDigest: authorizedCommandDigest,
      simulationDigest: authorizedSimulationDigest,
      approvalProof,
    } = authorization;
    if (
      approvalId === undefined ||
      approvalId.trim().length === 0 ||
      approvalApprovedAt === undefined ||
      approvalExpiresAt === undefined ||
      operatorId === undefined ||
      operatorId.trim().length === 0 ||
      authorizedCommandDigest !== simulation.command_digest ||
      authorizedSimulationDigest !== simulation.simulation_digest ||
      approvalProof === undefined ||
      this.#approvalProofVerifier === undefined
    ) {
      throw new GatewayError("VALID_APPROVAL_REQUIRED", "a bound signed approval is required");
    }
    const approvedAt = Date.parse(approvalApprovedAt);
    const expiresAt = Date.parse(approvalExpiresAt);
    const commandExpiry = Date.parse(command.expires_at);
    const simulationExpiry = Date.parse(simulation.simulated_at) + this.#simulationTtlMs;
    const now = this.#clock.now().getTime();
    if (
      !Number.isFinite(approvedAt) ||
      !Number.isFinite(expiresAt) ||
      approvedAt > now ||
      approvedAt < Date.parse(simulation.simulated_at) ||
      expiresAt <= now ||
      expiresAt <= approvedAt ||
      expiresAt > commandExpiry ||
      expiresAt > simulationExpiry
    ) {
      throw new GatewayError("VALID_APPROVAL_REQUIRED", "approval TTL is invalid");
    }
    const proofInput: ApprovalProofInput = {
      approvalId,
      approvedAt: new Date(approvedAt).toISOString(),
      expiresAt: new Date(expiresAt).toISOString(),
      operatorId,
      commandDigest: authorizedCommandDigest,
      simulationDigest: authorizedSimulationDigest,
    };
    if (!this.#approvalProofVerifier.verify(proofInput, approvalProof)) {
      throw new GatewayError("VALID_APPROVAL_REQUIRED", "approval proof is invalid", 401);
    }
  }

  #assertLivePromotion(command: DlmmCommandV1): void {
    const evidence = this.#livePromotion;
    const now = this.#clock.now().getTime();
    if (
      evidence === undefined ||
      evidence.promotionId.trim().length === 0 ||
      evidence.rulesetVersion !== command.ruleset_version ||
      !Number.isInteger(evidence.approvalCycles) ||
      evidence.approvalCycles < 10 ||
      !evidence.receiptsReconciled ||
      !evidence.riskInvariantsPreserved ||
      !evidence.dedicatedRpcReady ||
      !evidence.walletReady ||
      !evidence.checklistCompleted ||
      evidence.operatorConfirmationId.trim().length === 0 ||
      !Number.isFinite(Date.parse(evidence.promotedAt)) ||
      Date.parse(evidence.promotedAt) > now ||
      !Number.isFinite(Date.parse(evidence.expiresAt)) ||
      Date.parse(evidence.expiresAt) <= now
    ) {
      throw new GatewayError("LIVE_PROMOTION_REQUIRED", "LIVE promotion gate is not satisfied");
    }
  }

  #assertMeasuredSimulation(result: SimulationTransportResult): void {
    if (
      !Number.isInteger(result.priceImpactBps) ||
      !Number.isFinite(result.priceImpactBps) ||
      result.priceImpactBps < 0 ||
      result.priceImpactBps > 10_000 ||
      !Number.isInteger(result.computeUnits) ||
      result.computeUnits < 0 ||
      !Number.isInteger(result.lastValidBlockHeight) ||
      result.lastValidBlockHeight < 0 ||
      result.blockhash.length === 0
    ) {
      throw new GatewayError(
        "PRICE_IMPACT_UNAVAILABLE",
        "simulation lacks a measured finite price impact",
      );
    }
  }

  #normalizeError(error: unknown, fallbackCode: string): {
    code: string;
    message: string;
    retryable: boolean;
  } {
    if (error instanceof GatewayError) {
      return { code: error.code, message: error.message, retryable: false };
    }
    return {
      code: fallbackCode,
      message: "execution dependency rejected the command",
      retryable: true,
    };
  }

  async #withTimeout<T>(operation: Promise<T>): Promise<T> {
    let timeout: ReturnType<typeof setTimeout> | undefined;
    try {
      return await Promise.race([
        operation,
        new Promise<T>((_resolve, reject) => {
          timeout = setTimeout(
            () => reject(new Error("execution dependency timed out")),
            this.#operationTimeoutMs,
          );
        }),
      ]);
    } finally {
      if (timeout !== undefined) {
        clearTimeout(timeout);
      }
    }
  }
}

export function commandDigest(command: DlmmCommandV1): string {
  const normalized: Record<string, unknown> = {
    ...command,
    expires_at: normalizeTimestamp(command.expires_at),
  };
  for (const [key, value] of Object.entries(normalized)) {
    if (value === undefined) {
      delete normalized[key];
    }
  }
  return sha256(normalized);
}

export function simulationDigest(simulation: SimulationResultV1): string {
  const unsigned = { ...simulation } as Partial<SimulationResultV1>;
  delete unsigned.simulation_digest;
  return sha256({ ...unsigned, simulated_at: normalizeTimestamp(simulation.simulated_at) });
}

function finalizeSimulation(
  simulation: Omit<SimulationResultV1, "simulation_digest">,
): SimulationResultV1 {
  const unsigned = { ...simulation, simulation_digest: "0".repeat(64) };
  return { ...simulation, simulation_digest: simulationDigest(unsigned) };
}

function approvalProofMessage(input: ApprovalProofInput): string {
  return [
    input.approvalId,
    normalizeTimestamp(input.approvedAt),
    normalizeTimestamp(input.expiresAt),
    input.operatorId,
    input.commandDigest,
    input.simulationDigest,
  ].join("\n");
}

function normalizeTimestamp(value: string): string {
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) {
    throw new GatewayError("INVALID_TIMESTAMP", "timestamp is invalid", 400);
  }
  return new Date(parsed).toISOString();
}

function sha256(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(sortJson(value))).digest("hex");
}

function sortJson(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(sortJson);
  }
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .filter((entry) => entry[1] !== undefined)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, sortJson(item)]),
    );
  }
  return value;
}

function deepFreeze<T>(value: T): T {
  if (value !== null && typeof value === "object" && !Object.isFrozen(value)) {
    Object.freeze(value);
    for (const item of Object.values(value as Record<string, unknown>)) {
      deepFreeze(item);
    }
  }
  return value;
}

function cloneState(
  state: PersistedSubmission | undefined,
): PersistedSubmission | undefined {
  return state === undefined ? undefined : structuredClone(state);
}
