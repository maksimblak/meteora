import { createHash, createHmac, timingSafeEqual } from "node:crypto";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { dirname } from "node:path";

import {
  isPublicKey,
  type ChainReceiptV1,
  type DlmmCommandV2,
  type GatewayReceiptV2,
  type SettlementV2,
  type SimulationResultV2,
  type SwapQuoteV2,
} from "./contracts.js";

export interface Clock {
  now(): Date;
}

/** The official SDK's exact-in quote of a SWAP, as written into its transaction. */
export interface SwapFacts {
  inAmount: string;
  outAmount: string;
  /** The minimum output of the transaction: the quoted output less the command's slippage. */
  minOutAmount: string;
  feeAmount: string;
}

/** What the official SDK read from chain for one command (LB pair, mints and its quote). */
export interface MarketFacts {
  /**
   * Price impact of the command's swap leg from the SDK quote, rounded up to whole bps.
   * `null` only for an exit whose quote was unavailable: an exit swaps nothing, so its
   * impact is reported but never gates it; an entry and a SWAP need a measured value.
   */
  priceImpactBps: number | null;
  /** Active bin of the LB pair account. */
  activeBinId: number;
  /** Decimals of the X and Y mint accounts. */
  tokenXDecimals: number;
  tokenYDecimals: number;
  /** Mints of X and Y: the settlement of a confirmed command reads the wallet's changes. */
  tokenXMint?: string;
  tokenYMint?: string;
  /** Only for a SWAP: the quote it was built with. */
  swap?: SwapFacts;
}

/** Whose balances and which mints the settlement of a confirmed command is read for. */
export interface SettlementBinding {
  wallet: string;
  tokenXMint: string;
  tokenYMint: string;
}

/**
 * A command built by the SDK boundary. `opaqueSigners` may hold the key of a position the
 * boundary created for an OPEN: it lives only in this object, which the engine keeps in
 * memory until the command is broadcast or its simulation expires, and never serializes.
 */
export interface BuiltCommand {
  command: DlmmCommandV2;
  opaqueTransactions: readonly unknown[];
  opaqueSigners?: readonly unknown[];
  positionAddress: string;
  market: MarketFacts;
}

export interface SdkBoundary {
  build(command: DlmmCommandV2): Promise<BuiltCommand>;
}

export interface SimulationTransportResult {
  computeUnits: number;
  networkFeeLamports: bigint;
  priorityFeeLamports: bigint;
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
  /**
   * What the confirmed transactions did to the wallet; `undefined` while the chain
   * cannot tell yet (the receipt stays without a settlement and is read again).
   */
  settlement?(
    signatures: readonly string[],
    binding: SettlementBinding,
  ): Promise<SettlementV2 | undefined>;
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
  /** Addresses only; absent on submissions recorded before settlements existed. */
  settlementBinding?: SettlementBinding;
  /** The command's operation; absent on submissions recorded before F4. */
  operation?: DlmmCommandV2["operation"];
  /** A SWAP's exact-in amount (base units of X); the bound of the position's sale. */
  inAmount?: string;
  receipt: GatewayReceiptV2;
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
  /** Every submission whose receipt names `position` (its exits and its sales). */
  byPosition(position: string): Promise<PersistedSubmission[]>;
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

  async byPosition(position: string): Promise<PersistedSubmission[]> {
    return [...this.#byKey.values()]
      .filter((item) => item.receipt.position_address === position)
      .map((item) => cloneState(item) as PersistedSubmission);
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

// Version 2 holds V2 receipts; a document of another version is refused, never reinterpreted.
interface StateDocument {
  schemaVersion: 2;
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

  async byPosition(position: string): Promise<PersistedSubmission[]> {
    return this.#locked(async () => {
      const document = await this.#read();
      return document.submissions
        .filter((item) => item.receipt.position_address === position)
        .map((item) => cloneState(item) as PersistedSubmission);
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
      if (value.schemaVersion !== 2 || !Array.isArray(value.submissions)) {
        throw new Error("durable execution state has an unsupported version");
      }
      return value;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") {
        return { schemaVersion: 2, submissions: [] };
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
  simulation: SimulationResultV2;
  expiry: ReturnType<typeof setTimeout>;
}

interface Rejection {
  code: string;
  message: string;
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
  // Successful simulations only; for an OPEN this is the one place the new position's
  // key exists. An entry leaves on broadcast, re-simulation, failure or TTL expiry.
  readonly #prepared = new Map<string, PreparedCommand>();
  readonly #submissionsByKey = new Map<
    string,
    { commandDigest: string; promise: Promise<GatewayReceiptV2> }
  >();
  #salesTail: Promise<void> = Promise.resolve();

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

  /**
   * Build through the SDK, check the market facts against the command and only then
   * simulate the transactions. Market rejections (anchor drift, decimals, price impact
   * above the command limit) are failed simulations that carry the observed facts.
   */
  async simulate(command: DlmmCommandV2): Promise<SimulationResultV2> {
    this.#assertNotExpired(command);
    const immutableCommand = deepFreeze(structuredClone(command));
    const digest = commandDigest(immutableCommand);
    this.#release(immutableCommand.command_id);
    let market: MarketFacts | undefined;
    try {
      const sdkBuilt = await this.#withTimeout(this.#sdk.build(immutableCommand));
      if (commandDigest(sdkBuilt.command) !== digest || sdkBuilt.opaqueTransactions.length === 0) {
        throw new GatewayError("BUILD_BINDING_MISMATCH", "SDK build does not match the command");
      }
      market = measuredMarket(immutableCommand, sdkBuilt.market);
      assertPositionBinding(immutableCommand, sdkBuilt.positionAddress);
      const rejection = marketRejection(immutableCommand, market);
      if (rejection !== undefined) {
        return this.#failed(immutableCommand, digest, market, rejection);
      }
      const built: BuiltCommand = {
        command: immutableCommand,
        opaqueTransactions: Object.freeze([...sdkBuilt.opaqueTransactions]),
        ...(sdkBuilt.opaqueSigners === undefined
          ? {}
          : { opaqueSigners: Object.freeze([...sdkBuilt.opaqueSigners]) }),
        positionAddress: sdkBuilt.positionAddress,
        market,
      };
      const result = await this.#withTimeout(this.#rpc.simulate(built));
      assertCompleteSimulation(result);
      const normalized = deepFreeze(finalizeSimulation({
        schema_version: 2,
        command_id: immutableCommand.command_id,
        command_digest: digest,
        success: true,
        simulated_at: this.#clock.now().toISOString(),
        blockhash: result.blockhash,
        last_valid_block_height: result.lastValidBlockHeight,
        compute_units: result.computeUnits,
        network_fee_lamports: result.networkFeeLamports.toString(),
        priority_fee_lamports: result.priorityFeeLamports.toString(),
        price_impact_bps: market.priceImpactBps,
        min_bin_id: immutableCommand.min_bin_id ?? null,
        max_bin_id: immutableCommand.max_bin_id ?? null,
        position_address: built.positionAddress,
        active_bin_id: market.activeBinId,
        token_x_decimals: market.tokenXDecimals,
        token_y_decimals: market.tokenYDecimals,
        errors: [],
        ...swapQuoteOf(immutableCommand, market),
      }));
      const entry: PreparedCommand = {
        built,
        commandDigest: digest,
        simulation: normalized,
        expiry: setTimeout(() => this.#release(immutableCommand.command_id, entry), this.#simulationTtlMs),
      };
      entry.expiry.unref?.();
      this.#prepared.set(immutableCommand.command_id, entry);
      return structuredClone(normalized);
    } catch (error) {
      return this.#failed(
        immutableCommand,
        digest,
        market,
        this.#normalizeError(error, "SIMULATION_FAILED"),
      );
    }
  }

  async submit(
    command: DlmmCommandV2,
    idempotencyKey: string,
    authorization: SubmitAuthorization = { modeTokenVerified: false },
  ): Promise<GatewayReceiptV2> {
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
    try {
      this.#assertFreshSimulation(prepared.simulation);
    } catch (error) {
      this.#release(command.command_id, prepared);
      throw error;
    }
    if (
      isEntry(command) &&
      (prepared.simulation.price_impact_bps === null ||
        prepared.simulation.price_impact_bps > command.max_slippage_bps)
    ) {
      throw new GatewayError("SLIPPAGE_BREACH", "simulation exceeds the command slippage limit");
    }
    if (
      command.operation === "SWAP" &&
      (prepared.simulation.price_impact_bps === null ||
        prepared.simulation.swap_quote === undefined ||
        prepared.simulation.price_impact_bps > (command.max_price_impact_bps ?? -1))
    ) {
      throw new GatewayError("PRICE_IMPACT_BREACH", "simulation exceeds the swap impact limit");
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
      // Broadcast or not, the signed transactions and any new position key are done with.
      this.#release(command.command_id, prepared);
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

  async lookup(commandId: string): Promise<GatewayReceiptV2 | undefined> {
    return (await this.#store.getByCommand(commandId))?.receipt;
  }

  async get(commandId: string): Promise<GatewayReceiptV2 | undefined> {
    const state = await this.#store.getByCommand(commandId);
    return state === undefined ? undefined : this.#reconcileReceipt(state);
  }

  async #reconcileReceipt(state: PersistedSubmission): Promise<GatewayReceiptV2> {
    const receipt = state.receipt;
    if (receipt.state === "CONFIRMED") {
      return this.#settle(state);
    }
    if (receipt.state === "FAILED") {
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
    let next: GatewayReceiptV2["state"];
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
    return next === "CONFIRMED" ? this.#settle(updated) : updated.receipt;
  }

  /**
   * Attach what the chain says the confirmed command did to the wallet. Until the RPC can
   * tell, the receipt stays without a settlement and the next lookup reads it again.
   */
  async #settle(state: PersistedSubmission): Promise<GatewayReceiptV2> {
    const receipt = state.receipt;
    const binding = state.settlementBinding;
    if (
      (receipt.settlement !== undefined && receipt.settlement !== null) ||
      binding === undefined ||
      this.#rpc.settlement === undefined
    ) {
      return receipt;
    }
    let settlement: SettlementV2 | undefined;
    try {
      settlement = await this.#withTimeout(this.#rpc.settlement(receipt.signatures, binding));
    } catch {
      settlement = undefined;
    }
    if (settlement === undefined) {
      return receipt;
    }
    const updated: PersistedSubmission = { ...state, receipt: { ...receipt, settlement } };
    await this.#store.update(updated);
    return updated.receipt;
  }

  async #broadcast(
    command: DlmmCommandV2,
    digest: string,
    simulation: SimulationResultV2,
    idempotencyKey: string,
    built: BuiltCommand,
    approvalId: string | undefined,
  ): Promise<GatewayReceiptV2> {
    const transactionCount = built.opaqueTransactions.length;
    const submittedAt = this.#clock.now().toISOString();
    const { tokenXMint, tokenYMint } = built.market;
    let state: PersistedSubmission = {
      commandId: command.command_id,
      commandDigest: digest,
      simulationDigest: simulation.simulation_digest,
      idempotencyKey,
      ...(approvalId === undefined ? {} : { approvalId }),
      ...(tokenXMint === undefined || tokenYMint === undefined
        ? {}
        : { settlementBinding: { wallet: command.wallet_address, tokenXMint, tokenYMint } }),
      operation: command.operation,
      ...(command.in_amount === undefined ? {} : { inAmount: command.in_amount }),
      receipt: {
        schema_version: 2,
        command_id: command.command_id,
        command_digest: digest,
        simulation_digest: simulation.simulation_digest,
        idempotency_key: idempotencyKey,
        state: "UNKNOWN",
        // The simulated (and, in APPROVAL, approved) position; only an address leaves here.
        position_address: built.positionAddress,
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
    const reserved =
      command.operation === "SWAP"
        ? await this.#serializeSales(async () => {
            // A key already reserved is the same submission again, never a new part.
            if ((await this.#store.getByKey(idempotencyKey)) === undefined) {
              await this.#assertWithinExitReturns(command);
            }
            return this.#store.reserve(state);
          })
        : await this.#store.reserve(state);
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

  /**
   * Defense in depth of the sale after an exit (G22): the parts of a position's sale sum
   * to at most the base token its confirmed REMOVE/CLOSE returned - the `delta_x` of their
   * settlements in this store. A part counts by its settlement once sold, by its `in_amount`
   * while its outcome is open, not at all once FAILED. An exit not confirmed or not yet
   * settled returns nothing here, so the sale waits for it (the worker reads the same
   * settlement before it sells). A submission recorded before operations were stored counts
   * by the sign of its settlement: a positive `delta_x` returned, a negative one spent.
   */
  async #assertWithinExitReturns(command: DlmmCommandV2): Promise<void> {
    const position = command.position_address;
    const amount = command.in_amount;
    if (position === undefined || amount === undefined) {
      throw new GatewayError(EXCEEDS_EXIT_RETURNS, "a sale names its position and amount");
    }
    let returned = 0n;
    let committed = 0n;
    for (const item of await this.#store.byPosition(position)) {
      const { receipt } = item;
      const settlement = receipt.settlement;
      const delta =
        settlement === undefined || settlement === null ? undefined : BigInt(settlement.delta_x);
      const operation = item.operation ?? legacyOperation(delta);
      if (operation === "REMOVE" || operation === "CLOSE") {
        if (receipt.state === "CONFIRMED" && delta !== undefined && delta > 0n) {
          returned += delta;
        }
      } else if (operation === "SWAP" && receipt.state !== "FAILED") {
        committed += delta !== undefined && delta < 0n ? -delta : BigInt(item.inAmount ?? "0");
      }
    }
    if (committed + BigInt(amount) > returned) {
      throw new GatewayError(
        EXCEEDS_EXIT_RETURNS,
        "the sale exceeds the base token the position's confirmed exits returned",
      );
    }
  }

  /** One sale reservation at a time: the check above and its reservation are atomic. */
  async #serializeSales<T>(operation: () => Promise<T>): Promise<T> {
    const previous = this.#salesTail;
    let release: (() => void) | undefined;
    this.#salesTail = new Promise<void>((resolve) => {
      release = resolve;
    });
    await previous;
    try {
      return await operation();
    } finally {
      release?.();
    }
  }

  #assertNotExpired(command: DlmmCommandV2): void {
    const expiry = Date.parse(command.expires_at);
    if (!Number.isFinite(expiry) || expiry <= this.#clock.now().getTime()) {
      throw new GatewayError("COMMAND_EXPIRED", "command has expired");
    }
  }

  #assertPreparedBinding(
    prepared: PreparedCommand,
    command: DlmmCommandV2,
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

  #assertFreshSimulation(simulation: SimulationResultV2): void {
    const simulatedAt = Date.parse(simulation.simulated_at);
    const age = this.#clock.now().getTime() - simulatedAt;
    if (!Number.isFinite(simulatedAt) || age < 0 || age >= this.#simulationTtlMs) {
      throw new GatewayError("STALE_SIMULATION", "simulation is stale");
    }
  }

  #assertApproval(
    command: DlmmCommandV2,
    simulation: SimulationResultV2,
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

  #assertLivePromotion(command: DlmmCommandV2): void {
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

  #failed(
    command: DlmmCommandV2,
    digest: string,
    market: MarketFacts | undefined,
    error: { code: string; message: string; retryable?: boolean },
  ): SimulationResultV2 {
    return deepFreeze(finalizeSimulation({
      schema_version: 2,
      command_id: command.command_id,
      command_digest: digest,
      success: false,
      simulated_at: this.#clock.now().toISOString(),
      blockhash: null,
      last_valid_block_height: null,
      compute_units: 0,
      network_fee_lamports: "0",
      priority_fee_lamports: "0",
      price_impact_bps: market?.priceImpactBps ?? null,
      min_bin_id: command.min_bin_id ?? null,
      max_bin_id: command.max_bin_id ?? null,
      // A position that will never be opened is not published.
      position_address: null,
      active_bin_id: market?.activeBinId ?? null,
      token_x_decimals: market?.tokenXDecimals ?? null,
      token_y_decimals: market?.tokenYDecimals ?? null,
      errors: [{ code: error.code, message: error.message, retryable: error.retryable ?? false }],
      // A refused swap still shows the quote it was refused on (the size of the next part).
      ...(market === undefined ? {} : swapQuoteOf(command, market)),
    }));
  }

  /** Drop a prepared command (and with it any position key) unless it was replaced. */
  #release(commandId: string, expected?: PreparedCommand): void {
    const current = this.#prepared.get(commandId);
    if (current === undefined || (expected !== undefined && current !== expected)) {
      return;
    }
    clearTimeout(current.expiry);
    this.#prepared.delete(commandId);
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

/** A sale beyond what the position's confirmed exits returned (G22, defense in depth). */
const EXCEEDS_EXIT_RETURNS = "SWAP_EXCEEDS_EXIT_RETURNS";

/** The role of a submission stored without its operation, by what it did to X. */
function legacyOperation(deltaX: bigint | undefined): "REMOVE" | "SWAP" | undefined {
  if (deltaX === undefined || deltaX === 0n) {
    return undefined;
  }
  return deltaX > 0n ? "REMOVE" : "SWAP";
}

function isWhole(value: number, minimum: number, maximum: number): boolean {
  return Number.isInteger(value) && value >= minimum && value <= maximum;
}

function isEntry(command: DlmmCommandV2): boolean {
  return command.operation === "OPEN" || command.operation === "ADD";
}

/** Only an exit may go on without a measured impact: it swaps nothing (D08). */
function needsMeasuredImpact(command: DlmmCommandV2): boolean {
  return isEntry(command) || command.operation === "SWAP";
}

/** The simulation's `swap_quote` of a SWAP; nothing for any other operation. */
function swapQuoteOf(
  command: DlmmCommandV2,
  market: MarketFacts,
): { swap_quote: SwapQuoteV2 } | Record<string, never> {
  const swap = market.swap;
  if (command.operation !== "SWAP" || swap === undefined) {
    return {};
  }
  return {
    swap_quote: {
      in_amount: swap.inAmount,
      out_amount: swap.outAmount,
      min_out_amount: swap.minOutAmount,
      fee_amount: swap.feeAmount,
    },
  };
}

const wholeAmount = /^(0|[1-9][0-9]*)$/;

function measuredMarket(command: DlmmCommandV2, market: MarketFacts | undefined): MarketFacts {
  const impact = market?.priceImpactBps;
  if (
    market === undefined ||
    impact === undefined ||
    (impact === null ? needsMeasuredImpact(command) : !isWhole(impact, 0, 10_000))
  ) {
    throw new GatewayError(
      "PRICE_IMPACT_UNAVAILABLE",
      "the SDK quote did not measure a finite price impact",
    );
  }
  if (
    !Number.isSafeInteger(market.activeBinId) ||
    !isWhole(market.tokenXDecimals, 0, 255) ||
    !isWhole(market.tokenYDecimals, 0, 255)
  ) {
    throw new GatewayError("MARKET_STATE_UNAVAILABLE", "LB pair or mint state is unavailable");
  }
  if (command.operation === "SWAP") {
    const swap = market.swap;
    if (swap === undefined) {
      throw new GatewayError("PRICE_IMPACT_UNAVAILABLE", "the SDK did not quote the swap");
    }
    const amounts = [swap.inAmount, swap.outAmount, swap.minOutAmount, swap.feeAmount];
    if (!amounts.every((value) => wholeAmount.test(value))) {
      throw new GatewayError("SWAP_QUOTE_INCOMPLETE", "the SDK quote of the swap is not whole");
    }
  }
  return Object.freeze({ ...market });
}

function assertPositionBinding(command: DlmmCommandV2, positionAddress: string): void {
  const bound =
    command.operation === "OPEN"
      ? isPublicKey(positionAddress) && positionAddress !== command.wallet_address
      : positionAddress === command.position_address;
  if (!bound) {
    throw new GatewayError(
      "POSITION_BINDING_MISMATCH",
      "the built transaction does not act on the command's position",
    );
  }
}

/** The command's own limits checked against what the chain says right now. */
function marketRejection(command: DlmmCommandV2, market: MarketFacts): Rejection | undefined {
  if (command.operation === "SWAP") {
    return swapRejection(command, market);
  }
  if (!isEntry(command)) {
    // An exit swaps nothing: its quote is reported, never a reason to stay in (D08).
    return undefined;
  }
  const anchor = command.active_bin_id;
  const tolerance = command.active_bin_tolerance;
  if (
    anchor === undefined ||
    tolerance === undefined ||
    Math.abs(market.activeBinId - anchor) > tolerance
  ) {
    return {
      code: "ACTIVE_BIN_DRIFT",
      message: "pool active bin moved beyond the command tolerance",
    };
  }
  if (
    market.tokenXDecimals !== command.token_x_decimals ||
    market.tokenYDecimals !== command.token_y_decimals
  ) {
    return {
      code: "TOKEN_DECIMALS_MISMATCH",
      message: "mint decimals differ from the ones the amounts were computed with",
    };
  }
  if (market.priceImpactBps === null || market.priceImpactBps > command.max_slippage_bps) {
    return { code: "SLIPPAGE_BREACH", message: "quoted price impact exceeds the command limit" };
  }
  return undefined;
}

/**
 * A sale goes out only whole (the quote consumes exactly `in_amount`), with an output and
 * a non-zero minimum, and within its own impact limit; the caller sells a smaller part
 * after `PRICE_IMPACT_BREACH`.
 */
function swapRejection(command: DlmmCommandV2, market: MarketFacts): Rejection | undefined {
  const swap = market.swap;
  if (swap === undefined || market.priceImpactBps === null) {
    return { code: "PRICE_IMPACT_UNAVAILABLE", message: "the swap has no measured quote" };
  }
  if (
    swap.inAmount !== command.in_amount ||
    swap.outAmount === "0" ||
    swap.minOutAmount === "0" ||
    BigInt(swap.minOutAmount) > BigInt(swap.outAmount)
  ) {
    return {
      code: "SWAP_QUOTE_INCOMPLETE",
      message: "the quote does not sell the whole amount for a positive output",
    };
  }
  if (market.priceImpactBps > (command.max_price_impact_bps ?? -1)) {
    return {
      code: "PRICE_IMPACT_BREACH",
      message: "quoted price impact of the swap exceeds the command limit",
    };
  }
  return undefined;
}

function assertCompleteSimulation(result: SimulationTransportResult): void {
  if (
    !Number.isInteger(result.computeUnits) ||
    result.computeUnits < 0 ||
    !Number.isInteger(result.lastValidBlockHeight) ||
    result.lastValidBlockHeight < 0 ||
    result.blockhash.length === 0
  ) {
    throw new GatewayError("SIMULATION_INCOMPLETE", "transaction simulation is incomplete");
  }
}

export function commandDigest(command: DlmmCommandV2): string {
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

export function simulationDigest(simulation: SimulationResultV2): string {
  const unsigned = { ...simulation } as Partial<SimulationResultV2>;
  delete unsigned.simulation_digest;
  return sha256({ ...unsigned, simulated_at: normalizeTimestamp(simulation.simulated_at) });
}

function finalizeSimulation(
  simulation: Omit<SimulationResultV2, "simulation_digest">,
): SimulationResultV2 {
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
