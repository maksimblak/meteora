import {
  ComputeBudgetInstruction,
  ComputeBudgetProgram,
  Connection,
  Keypair,
  PublicKey,
  Transaction,
  type Commitment,
  type SendOptions,
  type TransactionInstruction,
} from "@solana/web3.js";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";

import type { DlmmCommandV2, SettlementV2 } from "./contracts.js";
import {
  InsufficientLiquidityError,
  type PoolQuote,
  type PoolQuoter,
  type PoolState,
} from "./quotes.js";
import {
  GatewayError,
  type BuiltCommand,
  type MarketFacts,
  type RpcStatus,
  type RpcTransport,
  type SettlementBinding,
  type SignatureRecorder,
  type SdkBoundary,
  type SimulationTransportResult,
} from "./engine.js";

const base58Alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";

/** A quote's price impact as the SDK reports it: a decimal.js percentage. */
export interface QuotedImpact {
  priceImpact: { toFixed(): string };
}

export interface SwapQuote extends QuotedImpact {
  consumedInAmount: { toString(): string };
  outAmount: { toString(): string };
  fee: { toString(): string };
}

/** What a SWAP needs of the quote: the bin arrays the swap instruction must list. */
export interface ExecutableSwapQuote extends SwapQuote {
  binArraysPubkey: PublicKey[];
}

/** The part of the official `DLMM` pool object this executor relies on. */
export interface MeteoraPoolClient {
  pubkey: PublicKey;
  lbPair: { activeId: number };
  tokenX: { publicKey: PublicKey; mint: { decimals: number } };
  tokenY: { publicKey: PublicKey; mint: { decimals: number } };
  getBinArrayForSwap(swapForY: boolean, count?: number): Promise<unknown[]>;
  swapQuote(
    inAmount: unknown,
    swapForY: boolean,
    allowedSlippage: unknown,
    binArrays: unknown[],
  ): QuotedImpact | SwapQuote | ExecutableSwapQuote;
  swap(parameters: {
    inToken: PublicKey;
    outToken: PublicKey;
    inAmount: unknown;
    minOutAmount: unknown;
    lbPair: PublicKey;
    user: PublicKey;
    binArraysPubkey: PublicKey[];
  }): Promise<Transaction>;
  swapQuoteExactOut(
    outAmount: unknown,
    swapForY: boolean,
    allowedSlippage: unknown,
    binArrays: unknown[],
  ): QuotedImpact;
  getPosition(position: PublicKey): Promise<{ positionData: { totalXAmount: string } }>;
  initializePositionAndAddLiquidityByStrategy(parameters: {
    positionPubKey: PublicKey;
    totalXAmount: unknown;
    totalYAmount: unknown;
    strategy: { minBinId: number; maxBinId: number; strategyType: number };
    user: PublicKey;
    slippage: number;
  }): Promise<Transaction>;
  addLiquidityByStrategy(parameters: {
    positionPubKey: PublicKey;
    totalXAmount: unknown;
    totalYAmount: unknown;
    strategy: { minBinId: number; maxBinId: number; strategyType: number };
    user: PublicKey;
    slippage: number;
  }): Promise<Transaction>;
  removeLiquidity(parameters: {
    user: PublicKey;
    position: PublicKey;
    fromBinId: number;
    toBinId: number;
    bps: unknown;
    shouldClaimAndClose: boolean;
  }): Promise<Transaction[]>;
}

export interface MeteoraDlmmFactory {
  create(connection: Connection, pool: PublicKey): Promise<MeteoraPoolClient>;
  StrategyType: { Spot: number; BidAsk: number };
}

interface BigNumberConstructor {
  new (value: string | number): unknown;
}

const require = createRequire(import.meta.url);
const MeteoraDlmm = require("@meteora-ag/dlmm") as MeteoraDlmmFactory & {
  prototype: Record<string, unknown>;
};
const BN = require("bn.js") as BigNumberConstructor;

const quoteMethods = ["swapQuote", "swapQuoteExactOut", "getBinArrayForSwap", "getPosition", "swap"];

/** True when the installed official SDK exposes the quote API price impact is measured with. */
export function officialQuotesAvailable(): boolean {
  return (
    typeof MeteoraDlmm.create === "function" &&
    quoteMethods.every((name) => typeof MeteoraDlmm.prototype[name] === "function")
  );
}

/**
 * Whole bps of a quoted price impact given in percent, rounded up (against the trade) and
 * saturated at 100 %. Anything that is not a finite non-negative decimal is refused.
 */
export function priceImpactBps(percent: string): number {
  const match = /^(\d+)(?:\.(\d+))?$/.exec(percent.trim());
  if (match === null) {
    throw new GatewayError("PRICE_IMPACT_UNAVAILABLE", "the SDK quote has no finite price impact");
  }
  const whole = BigInt(match[1] ?? "0");
  const fraction = match[2] ?? "";
  const hundredths = BigInt(fraction.slice(0, 2).padEnd(2, "0"));
  const remainder = /[1-9]/.test(fraction.slice(2)) ? 1n : 0n;
  const bps = whole * 100n + hundredths + remainder;
  return bps > 10_000n ? 10_000 : Number(bps);
}

/** A u64-like integer amount from an SDK string (decimal.js may emit a fraction or exponent). */
function wholeUnits(value: string): bigint {
  const match = /^(\d+)(?:\.(\d*))?(?:e\+?(\d+))?$/i.exec(value.trim());
  if (match === null) {
    throw new GatewayError("POSITION_STATE_UNAVAILABLE", "position amount is not a whole number");
  }
  const digits = (match[1] ?? "") + (match[2] ?? "");
  const shift = Number(match[3] ?? "0") - (match[2] ?? "").length;
  return shift >= 0 ? BigInt(digits) * 10n ** BigInt(shift) : BigInt(digits.slice(0, shift) || "0");
}

async function quoted(measure: () => QuotedImpact | Promise<QuotedImpact>): Promise<number> {
  let quote: QuotedImpact;
  try {
    quote = await measure();
  } catch {
    // Insufficient liquidity or a failed fetch: the impact is unknown, so the trade is not made.
    throw new GatewayError("PRICE_IMPACT_UNAVAILABLE", "the official SDK quote is unavailable");
  }
  return priceImpactBps(quote.priceImpact.toFixed());
}

export interface SecretSignerProvider {
  loadWalletSigner(): Promise<Keypair>;
}

export class FileSecretSignerProvider implements SecretSignerProvider {
  constructor(private readonly walletSecretPath: string) {}

  async loadWalletSigner(): Promise<Keypair> {
    try {
      const parsed: unknown = JSON.parse(await readFile(this.walletSecretPath, "utf8"));
      if (
        !Array.isArray(parsed) ||
        parsed.length !== 64 ||
        parsed.some((value) => !Number.isInteger(value) || value < 0 || value > 255)
      ) {
        throw new Error("invalid secret material");
      }
      return Keypair.fromSecretKey(Uint8Array.from(parsed as number[]));
    } catch {
      throw new Error("secret signer is unavailable or invalid");
    }
  }
}

export type PositionKeyFactory = () => Keypair;

/**
 * Builds commands with the official SDK. For an OPEN it creates the key of the new
 * position, which only ever lives inside the returned `BuiltCommand`; ADD, REMOVE and
 * CLOSE act on the command's existing position and need the wallet signature only.
 */
export class OfficialMeteoraDlmmBoundary implements SdkBoundary {
  constructor(
    private readonly connection: Connection,
    private readonly signers: SecretSignerProvider,
    private readonly factory: MeteoraDlmmFactory = MeteoraDlmm,
    private readonly newPositionKey: PositionKeyFactory = () => Keypair.generate(),
  ) {}

  async build(command: DlmmCommandV2): Promise<BuiltCommand> {
    const wallet = await this.signers.loadWalletSigner();
    if (wallet.publicKey.toBase58() !== command.wallet_address) {
      throw new Error("configured wallet does not match command wallet");
    }
    const pool = await this.factory.create(this.connection, new PublicKey(command.pool_address));
    const marketBase = {
      activeBinId: pool.lbPair.activeId,
      tokenXDecimals: pool.tokenX.mint.decimals,
      tokenYDecimals: pool.tokenY.mint.decimals,
      tokenXMint: pool.tokenX.publicKey.toBase58(),
      tokenYMint: pool.tokenY.publicKey.toBase58(),
    };
    if (command.operation === "SWAP") {
      return this.#swap(pool, wallet, command, marketBase);
    }
    const { min_bin_id: minBinId, max_bin_id: maxBinId } = command;
    if (minBinId === undefined || maxBinId === undefined) {
      throw new Error("a liquidity command requires its bin range");
    }
    const strategy = {
      minBinId,
      maxBinId,
      strategyType:
        command.strategy === "BID_ASK"
          ? this.factory.StrategyType.BidAsk
          : this.factory.StrategyType.Spot,
    };
    const slippage = command.max_slippage_bps / 100;

    if (command.operation === "OPEN") {
      const positionKey = this.newPositionKey();
      const transaction = await pool.initializePositionAndAddLiquidityByStrategy({
        positionPubKey: positionKey.publicKey,
        totalXAmount: new BN(command.amount_x ?? "0"),
        totalYAmount: new BN(command.amount_y ?? "0"),
        strategy,
        user: wallet.publicKey,
        slippage,
      });
      return {
        command,
        opaqueTransactions: [transaction],
        opaqueSigners: [wallet, positionKey],
        positionAddress: positionKey.publicKey.toBase58(),
        market: { ...marketBase, priceImpactBps: await this.#entryImpact(pool, command) },
      };
    }
    const positionAddress = command.position_address;
    if (positionAddress === undefined) {
      throw new Error("position address is required");
    }
    const position = new PublicKey(positionAddress);
    let transactions: Transaction[];
    let impact: number | null;
    if (command.operation === "ADD") {
      transactions = [
        await pool.addLiquidityByStrategy({
          positionPubKey: position,
          totalXAmount: new BN(command.amount_x ?? "0"),
          totalYAmount: new BN(command.amount_y ?? "0"),
          strategy,
          user: wallet.publicKey,
          slippage,
        }),
      ];
      impact = await this.#entryImpact(pool, command);
    } else {
      transactions = await pool.removeLiquidity({
        user: wallet.publicKey,
        position,
        fromBinId: minBinId,
        toBinId: maxBinId,
        bps: new BN(command.remove_bps ?? 0),
        shouldClaimAndClose: command.operation === "CLOSE",
      });
      impact = await this.#exitImpact(pool, command, position);
    }
    const market: MarketFacts = { ...marketBase, priceImpactBps: impact };
    return {
      command,
      opaqueTransactions: transactions,
      opaqueSigners: [wallet],
      positionAddress,
      market,
    };
  }

  /**
   * The sale of the pool's X for Y in the same pool (G22): the official exact-in quote
   * measures the impact and the output; the transaction is the official `swap` over the
   * quote's bin arrays with the quoted output less the command's slippage as its
   * minimum, so the chain refuses a worse fill. A quote that cannot be made refuses the
   * swap (`PRICE_IMPACT_UNAVAILABLE`) - a sale is never sent unmeasured.
   */
  async #swap(
    pool: MeteoraPoolClient,
    wallet: Keypair,
    command: DlmmCommandV2,
    marketBase: Omit<MarketFacts, "priceImpactBps">,
  ): Promise<BuiltCommand> {
    const positionAddress = command.position_address;
    if (positionAddress === undefined || command.in_amount === undefined) {
      throw new Error("a swap names its position and amount");
    }
    const inAmount = new BN(command.in_amount);
    let quote: ExecutableSwapQuote;
    try {
      const quoted = pool.swapQuote(
        inAmount,
        true,
        new BN(command.max_slippage_bps),
        await pool.getBinArrayForSwap(true),
      );
      if (!("outAmount" in quoted) || !("binArraysPubkey" in quoted)) {
        throw new Error("the quote has no output or bin arrays");
      }
      quote = quoted;
    } catch {
      throw new GatewayError("PRICE_IMPACT_UNAVAILABLE", "the official SDK quote is unavailable");
    }
    let consumed: bigint;
    let outAmount: bigint;
    let fee: bigint;
    try {
      consumed = wholeUnits(quote.consumedInAmount.toString());
      outAmount = wholeUnits(quote.outAmount.toString());
      fee = wholeUnits(quote.fee.toString());
    } catch {
      throw new GatewayError("SWAP_QUOTE_INCOMPLETE", "the official SDK quote is not whole");
    }
    const minOutAmount = (outAmount * BigInt(10_000 - command.max_slippage_bps)) / 10_000n;
    const priceImpact = priceImpactBps(quote.priceImpact.toFixed());
    const transaction = await pool.swap({
      inToken: pool.tokenX.publicKey,
      outToken: pool.tokenY.publicKey,
      inAmount,
      minOutAmount: new BN(minOutAmount.toString()),
      lbPair: pool.pubkey,
      user: wallet.publicKey,
      binArraysPubkey: quote.binArraysPubkey,
    });
    return {
      command,
      opaqueTransactions: [transaction],
      opaqueSigners: [wallet],
      positionAddress,
      market: {
        ...marketBase,
        priceImpactBps: priceImpact,
        swap: {
          inAmount: consumed.toString(),
          outAmount: outAmount.toString(),
          minOutAmount: minOutAmount.toString(),
          feeAmount: fee.toString(),
        },
      },
    };
  }

  /** Entry swap leg: acquiring the command's X amount with Y (exact-out quote). */
  async #entryImpact(pool: MeteoraPoolClient, command: DlmmCommandV2): Promise<number> {
    const amountX = BigInt(command.amount_x ?? "0");
    if (amountX === 0n) {
      return 0;
    }
    return quoted(async () =>
      pool.swapQuoteExactOut(
        new BN(amountX.toString()),
        false,
        new BN(command.max_slippage_bps),
        await pool.getBinArrayForSwap(false),
      ),
    );
  }

  /**
   * Exit swap leg: selling the X this command withdraws from the position for Y. Best
   * effort - the exit itself swaps nothing, so an unavailable quote is `null`, never a
   * reason to keep the position open (D08).
   */
  async #exitImpact(
    pool: MeteoraPoolClient,
    command: DlmmCommandV2,
    position: PublicKey,
  ): Promise<number | null> {
    try {
      const totalX = wholeUnits((await pool.getPosition(position)).positionData.totalXAmount);
      const withdrawnX = (totalX * BigInt(command.remove_bps ?? 0)) / 10_000n;
      if (withdrawnX === 0n) {
        return 0;
      }
      return await quoted(async () =>
        pool.swapQuote(
          new BN(withdrawnX.toString()),
          true,
          new BN(command.max_slippage_bps),
          await pool.getBinArrayForSwap(true),
        ),
      );
    } catch {
      return null;
    }
  }
}

/** Read-only exact-in quote of the official SDK, the same quote that measures exits. */
export class OfficialMeteoraQuoter implements PoolQuoter {
  constructor(
    private readonly connection: Connection,
    private readonly factory: MeteoraDlmmFactory = MeteoraDlmm,
  ) {}

  async quote(pool: string, swapForY: boolean, inAmount: bigint): Promise<PoolQuote> {
    const client = await this.factory.create(this.connection, new PublicKey(pool));
    const market: PoolState = {
      activeBinId: client.lbPair.activeId,
      tokenXDecimals: client.tokenX.mint.decimals,
      tokenYDecimals: client.tokenY.mint.decimals,
    };
    const binArrays = await client.getBinArrayForSwap(swapForY);
    let quote: QuotedImpact | SwapQuote;
    try {
      quote = client.swapQuote(new BN(inAmount.toString()), swapForY, new BN(0), binArrays);
    } catch (error) {
      if (error instanceof Error && error.name === "SWAP_QUOTE_INSUFFICIENT_LIQUIDITY") {
        throw new InsufficientLiquidityError(market);
      }
      throw error;
    }
    if (!("outAmount" in quote)) {
      throw new GatewayError("QUOTE_FAILED", "the SDK quote has no output amount");
    }
    return {
      ...market,
      consumedInAmount: wholeUnits(quote.consumedInAmount.toString()).toString(),
      outAmount: wholeUnits(quote.outAmount.toString()).toString(),
      feeAmount: wholeUnits(quote.fee.toString()).toString(),
      priceImpactBps: priceImpactBps(quote.priceImpact.toFixed()),
    };
  }
}

/** What a money mode needs before the executor may call itself ready. */
export interface LiveReadiness {
  wallet: boolean;
  rpc: boolean;
  priceImpact: boolean;
}

export interface LiveReadinessProbe {
  check(): Promise<LiveReadiness>;
}

/**
 * Re-checked on every readiness request: the wallet file still loads (and is the expected
 * public key), the RPC answers within the timeout and the official quote API is present.
 */
export class SolanaLiveReadiness implements LiveReadinessProbe {
  constructor(
    private readonly connection: Connection,
    private readonly signers: SecretSignerProvider,
    private readonly expectedWallet: string | undefined,
    private readonly quotesAvailable: () => boolean = officialQuotesAvailable,
    private readonly timeoutMs = 2_000,
  ) {}

  async check(): Promise<LiveReadiness> {
    const [wallet, rpc] = await Promise.all([this.#wallet(), this.#rpc()]);
    return { wallet, rpc, priceImpact: this.quotesAvailable() };
  }

  async #wallet(): Promise<boolean> {
    try {
      const signer = await this.signers.loadWalletSigner();
      return this.expectedWallet === undefined || signer.publicKey.toBase58() === this.expectedWallet;
    } catch {
      return false;
    }
  }

  async #rpc(): Promise<boolean> {
    let timeout: ReturnType<typeof setTimeout> | undefined;
    try {
      await Promise.race([
        this.connection.getLatestBlockhash("confirmed"),
        new Promise<never>((_resolve, reject) => {
          timeout = setTimeout(() => reject(new Error("RPC timed out")), this.timeoutMs);
        }),
      ]);
      return true;
    } catch {
      return false;
    } finally {
      if (timeout !== undefined) {
        clearTimeout(timeout);
      }
    }
  }
}

/**
 * Priority fee of every executor transaction (§6.8, No 49): the price of a compute unit is
 * the configured percentile of the network's recent prioritization fees for the pool, never
 * below `minMicroLamports` (also the price when there is no data) and never so high that one
 * transaction pays more than `maxLamports` (0.015 SOL by default, as in the guide).
 */
export interface PriorityFeePolicy {
  percentile: number;
  minMicroLamports: bigint;
  maxLamports: bigint;
}

export const DEFAULT_PRIORITY_FEE_POLICY: PriorityFeePolicy = Object.freeze({
  percentile: 75,
  minMicroLamports: 1_000n,
  maxLamports: 15_000_000n,
});

const MICRO_LAMPORTS_PER_LAMPORT = 1_000_000n;
// Without a limit instruction the runtime grants 200 000 CU per (non compute-budget)
// instruction, at most 1 400 000 per transaction.
const DEFAULT_UNITS_PER_INSTRUCTION = 200_000;
const MAX_UNITS_PER_TRANSACTION = 1_400_000;
// The base fee of one signature; the rest of a confirmed transaction's fee is its priority fee.
const LAMPORTS_PER_SIGNATURE = 5_000n;

/** Micro-lamports per compute unit for a transaction granted `computeUnitLimit` units. */
export function computeUnitPrice(
  recentFees: readonly number[],
  computeUnitLimit: number,
  policy: PriorityFeePolicy,
): bigint {
  const fees = recentFees
    .filter((fee) => Number.isSafeInteger(fee) && fee >= 0)
    .sort((left, right) => left - right);
  let price = policy.minMicroLamports;
  if (fees.length > 0) {
    // Nearest-rank percentile.
    const rank = Math.max(Math.ceil((policy.percentile / 100) * fees.length), 1);
    const observed = BigInt(fees[rank - 1] ?? 0);
    if (observed > price) {
      price = observed;
    }
  }
  const cap = (policy.maxLamports * MICRO_LAMPORTS_PER_LAMPORT) / BigInt(Math.max(computeUnitLimit, 1));
  return price < cap ? price : cap;
}

/** The priority fee a transaction pays at `price` for its compute unit limit, rounded up. */
export function priorityFeeLamports(price: bigint, computeUnitLimit: number): bigint {
  const microLamports = price * BigInt(computeUnitLimit);
  return (microLamports + MICRO_LAMPORTS_PER_LAMPORT - 1n) / MICRO_LAMPORTS_PER_LAMPORT;
}

/**
 * `PRIORITY_FEE_MAX_LAMPORTS`, `PRIORITY_FEE_PERCENTILE`, `PRIORITY_FEE_MIN_MICRO_LAMPORTS`;
 * empty means the default. A value that is not a whole number in range throws: an executor
 * with a misread fee ceiling must not start trading.
 */
export function priorityFeePolicyFromEnvironment(
  environ: NodeJS.ProcessEnv | Record<string, string | undefined>,
): PriorityFeePolicy {
  const whole = (name: string, fallback: bigint, minimum: bigint, maximum?: bigint): bigint => {
    const raw = environ[name]?.trim() ?? "";
    if (raw.length === 0) {
      return fallback;
    }
    if (!/^(0|[1-9][0-9]*)$/.test(raw)) {
      throw new Error(`${name} must be a whole number`);
    }
    const value = BigInt(raw);
    if (value < minimum || (maximum !== undefined && value > maximum)) {
      throw new Error(`${name} is out of range`);
    }
    return value;
  };
  return {
    percentile: Number(
      whole("PRIORITY_FEE_PERCENTILE", BigInt(DEFAULT_PRIORITY_FEE_POLICY.percentile), 1n, 100n),
    ),
    minMicroLamports: whole(
      "PRIORITY_FEE_MIN_MICRO_LAMPORTS",
      DEFAULT_PRIORITY_FEE_POLICY.minMicroLamports,
      0n,
    ),
    maxLamports: whole("PRIORITY_FEE_MAX_LAMPORTS", DEFAULT_PRIORITY_FEE_POLICY.maxLamports, 0n),
  };
}

function isComputeBudget(instruction: TransactionInstruction): boolean {
  return instruction.programId.equals(ComputeBudgetProgram.programId);
}

function computeBudgetKind(instruction: TransactionInstruction): string | undefined {
  try {
    return ComputeBudgetInstruction.decodeInstructionType(instruction);
  } catch {
    return undefined;
  }
}

/** The compute unit limit the runtime grants this transaction. */
function computeUnitLimitOf(transaction: Transaction): number {
  for (const instruction of transaction.instructions) {
    if (isComputeBudget(instruction) && computeBudgetKind(instruction) === "SetComputeUnitLimit") {
      return ComputeBudgetInstruction.decodeSetComputeUnitLimit(instruction).units;
    }
  }
  const instructions = transaction.instructions.filter((item) => !isComputeBudget(item)).length;
  return Math.min(Math.max(instructions, 1) * DEFAULT_UNITS_PER_INSTRUCTION, MAX_UNITS_PER_TRANSACTION);
}

/** One price instruction per transaction, right after the limit the SDK set (if any). */
function setComputeUnitPrice(transaction: Transaction, microLamports: bigint): void {
  transaction.instructions = transaction.instructions.filter(
    (item) => !(isComputeBudget(item) && computeBudgetKind(item) === "SetComputeUnitPrice"),
  );
  const price = ComputeBudgetProgram.setComputeUnitPrice({ microLamports });
  const limitIndex = transaction.instructions.findIndex(
    (item) => isComputeBudget(item) && computeBudgetKind(item) === "SetComputeUnitLimit",
  );
  transaction.instructions.splice(limitIndex + 1, 0, price);
}

/** A token balance of a confirmed transaction as the RPC reports it. */
export interface ChainTokenBalance {
  accountIndex: number;
  mint: string;
  owner?: string;
  uiTokenAmount: { amount: string };
}

/** The facts of one confirmed transaction the settlement is computed from. */
export interface SettledTransaction {
  fee: bigint;
  signatureCount: number;
  walletIndex: number;
  preBalances: readonly number[];
  postBalances: readonly number[];
  preTokenBalances: readonly ChainTokenBalance[];
  postTokenBalances: readonly ChainTokenBalance[];
}

export const WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112";

/**
 * What a confirmed command did to the wallet: the fee it paid, the part of it above the
 * signature fees, and the signed changes of its X and Y balances over all its
 * transactions. A wrapped-SOL leg also counts the native lamports the wallet received,
 * net of the fee and of the rent its token accounts were created or closed with (the SDK
 * unwraps SOL into the wallet).
 */
export function walletSettlement(
  transactions: readonly SettledTransaction[],
  binding: SettlementBinding,
): SettlementV2 {
  let fee = 0n;
  let priority = 0n;
  let deltaX = 0n;
  let deltaY = 0n;
  for (const transaction of transactions) {
    fee += transaction.fee;
    const base = LAMPORTS_PER_SIGNATURE * BigInt(transaction.signatureCount);
    priority += transaction.fee > base ? transaction.fee - base : 0n;
    deltaX += legDelta(transaction, binding.wallet, binding.tokenXMint);
    deltaY += legDelta(transaction, binding.wallet, binding.tokenYMint);
  }
  return {
    fee_lamports: fee.toString(),
    priority_fee_lamports: priority.toString(),
    delta_x: deltaX.toString(),
    delta_y: deltaY.toString(),
    mint_x: binding.tokenXMint,
    mint_y: binding.tokenYMint,
  };
}

function legDelta(transaction: SettledTransaction, wallet: string, mint: string): bigint {
  const owned = (balances: readonly ChainTokenBalance[]): ChainTokenBalance[] =>
    balances.filter((item) => item.owner === wallet);
  const pre = owned(transaction.preTokenBalances);
  const post = owned(transaction.postTokenBalances);
  const amount = (balances: ChainTokenBalance[], index: number, ofMint?: string): bigint =>
    balances
      .filter((item) => item.accountIndex === index && (ofMint === undefined || item.mint === ofMint))
      .reduce((total, item) => total + BigInt(item.uiTokenAmount.amount), 0n);
  const sum = (balances: ChainTokenBalance[]): bigint =>
    balances
      .filter((item) => item.mint === mint)
      .reduce((total, item) => total + BigInt(item.uiTokenAmount.amount), 0n);
  let delta = sum(post) - sum(pre);
  if (mint !== WRAPPED_SOL_MINT) {
    return delta;
  }
  const lamports = (balances: readonly number[], index: number): bigint =>
    BigInt(balances[index] ?? 0);
  delta +=
    lamports(transaction.postBalances, transaction.walletIndex) -
    lamports(transaction.preBalances, transaction.walletIndex) +
    transaction.fee;
  const accounts = new Set([...pre, ...post].map((item) => item.accountIndex));
  for (const index of accounts) {
    // Rent is what a token account holds beyond its wrapped SOL.
    const rentBefore = lamports(transaction.preBalances, index) - amount(pre, index, WRAPPED_SOL_MINT);
    const rentAfter = lamports(transaction.postBalances, index) - amount(post, index, WRAPPED_SOL_MINT);
    delta += rentAfter - rentBefore;
  }
  return delta;
}

export class SolanaRpcTransport implements RpcTransport {
  constructor(
    private readonly connection: Connection,
    private readonly commitment: Commitment = "confirmed",
    private readonly sendOptions: SendOptions = { maxRetries: 0, skipPreflight: true },
    private readonly priorityFees: PriorityFeePolicy = DEFAULT_PRIORITY_FEE_POLICY,
  ) {}

  async simulate(command: BuiltCommand): Promise<SimulationTransportResult> {
    const transactions = this.#transactions(command);
    const signers = this.#signers(command);
    const feePayer = signers[0];
    if (feePayer === undefined) {
      throw new Error("wallet signer is unavailable");
    }
    const latest = await this.connection.getLatestBlockhash(this.commitment);
    const recentFees = await this.#recentFees(command.command.pool_address);
    let computeUnits = 0;
    let networkFeeLamports = 0n;
    let priorityFee = 0n;

    for (const transaction of transactions) {
      transaction.recentBlockhash = latest.blockhash;
      transaction.feePayer = feePayer.publicKey;
      // The base fee is quoted before the price instruction: the priority fee is its own figure.
      const fee = await this.connection.getFeeForMessage(
        transaction.compileMessage(),
        this.commitment,
      );
      networkFeeLamports += BigInt(fee.value ?? 0);
      const limit = computeUnitLimitOf(transaction);
      const price = computeUnitPrice(recentFees, limit, this.priorityFees);
      setComputeUnitPrice(transaction, price);
      priorityFee += priorityFeeLamports(price, limit);
      // Every signer must be named by the transaction: an OPEN is signed by its new position key.
      transaction.partialSign(...signers);
      const result = await this.connection.simulateTransaction(transaction);
      if (result.value.err !== null) {
        throw simulationFailure(result.value.logs ?? []);
      }
      computeUnits += result.value.unitsConsumed ?? 0;
    }

    return {
      computeUnits,
      networkFeeLamports,
      priorityFeeLamports: priorityFee,
      blockhash: latest.blockhash,
      lastValidBlockHeight: latest.lastValidBlockHeight,
      logs: [],
    };
  }

  /**
   * Fees of the slots that recently locked the pool's account; no data (an RPC without the
   * method, an error, an empty answer) means the configured minimum price.
   */
  async #recentFees(pool: string): Promise<number[]> {
    try {
      const fees = await this.connection.getRecentPrioritizationFees({
        lockedWritableAccounts: [new PublicKey(pool)],
      });
      return Array.isArray(fees) ? fees.map((item) => item.prioritizationFee) : [];
    } catch {
      return [];
    }
  }

  async settlement(
    signatures: readonly string[],
    binding: SettlementBinding,
  ): Promise<SettlementV2 | undefined> {
    const facts: SettledTransaction[] = [];
    for (const signature of signatures) {
      const response = await this.connection.getTransaction(signature, {
        commitment: "confirmed",
        maxSupportedTransactionVersion: 0,
      });
      const meta = response?.meta;
      if (response === null || response === undefined || meta === null || meta === undefined) {
        return undefined;
      }
      const keys = response.transaction.message.getAccountKeys(
        meta.loadedAddresses === undefined ? undefined : { accountKeysFromLookups: meta.loadedAddresses },
      );
      let walletIndex = -1;
      for (let index = 0; index < keys.length; index += 1) {
        if (keys.get(index)?.toBase58() === binding.wallet) {
          walletIndex = index;
          break;
        }
      }
      if (walletIndex < 0) {
        return undefined;
      }
      facts.push({
        fee: BigInt(meta.fee),
        signatureCount: response.transaction.signatures.length,
        walletIndex,
        preBalances: meta.preBalances,
        postBalances: meta.postBalances,
        preTokenBalances: meta.preTokenBalances ?? [],
        postTokenBalances: meta.postTokenBalances ?? [],
      });
    }
    return walletSettlement(facts, binding);
  }

  async submit(
    command: BuiltCommand,
    recordSignature?: SignatureRecorder,
  ): Promise<readonly string[]> {
    const signatures: string[] = [];
    for (const transaction of this.#transactions(command)) {
      const signatureBytes = transaction.signature;
      if (signatureBytes === null) {
        throw new Error("signed transaction has no deterministic signature");
      }
      const signature = encodeBase58(signatureBytes);
      await recordSignature?.(signature);
      const rpcSignature = await this.connection.sendRawTransaction(
        transaction.serialize(),
        this.sendOptions,
      );
      if (rpcSignature !== signature) {
        throw new Error("RPC returned a signature that differs from the signed transaction");
      }
      signatures.push(signature);
    }
    return signatures;
  }

  async status(signature: string): Promise<RpcStatus> {
    const response = await this.connection.getSignatureStatuses([signature], {
      searchTransactionHistory: true,
    });
    const status = response.value[0];
    if (status === null || status === undefined) {
      return { state: "UNKNOWN", slot: null };
    }
    if (status.err !== null) {
      return { state: "FAILED", slot: status.slot, error: "on-chain transaction failed" };
    }
    if (status.confirmationStatus === "confirmed" || status.confirmationStatus === "finalized") {
      return { state: "CONFIRMED", slot: status.slot };
    }
    return { state: "PENDING", slot: status.slot };
  }

  #transactions(command: BuiltCommand): Transaction[] {
    if (!command.opaqueTransactions.every((value) => value instanceof Transaction)) {
      throw new Error("SDK returned an unsupported transaction type");
    }
    return [...command.opaqueTransactions];
  }

  #signers(command: BuiltCommand): Keypair[] {
    const values = command.opaqueSigners ?? [];
    if (!values.every((value) => value instanceof Keypair)) {
      throw new Error("SDK returned an unsupported signer type");
    }
    if (values.length === 0) {
      throw new Error("wallet signer is unavailable");
    }
    return [...values];
  }
}

/** A failed transaction simulation as an error code the operator can act on. */
function simulationFailure(logs: readonly string[]): GatewayError {
  if (logs.some((line) => /insufficient (funds|lamports)/i.test(line))) {
    return new GatewayError(
      "INSUFFICIENT_FUNDS",
      "the wallet balance does not cover the transaction",
    );
  }
  return new GatewayError("TRANSACTION_SIMULATION_FAILED", "transaction simulation failed");
}

function encodeBase58(bytes: Uint8Array): string {
  if (bytes.length === 0) {
    return "";
  }
  const digits = [0];
  for (const byte of bytes) {
    let carry = byte;
    for (let index = 0; index < digits.length; index += 1) {
      carry += (digits[index] ?? 0) << 8;
      digits[index] = carry % 58;
      carry = Math.floor(carry / 58);
    }
    while (carry > 0) {
      digits.push(carry % 58);
      carry = Math.floor(carry / 58);
    }
  }
  let encoded = "";
  for (let index = 0; index < bytes.length - 1 && bytes[index] === 0; index += 1) {
    encoded += "1";
  }
  for (let index = digits.length - 1; index >= 0; index -= 1) {
    encoded += base58Alphabet[digits[index] ?? 0];
  }
  return encoded;
}
