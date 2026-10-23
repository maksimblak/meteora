import {
  Connection,
  Keypair,
  PublicKey,
  Transaction,
  type Commitment,
  type SendOptions,
} from "@solana/web3.js";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";

import type { DlmmCommandV2 } from "./contracts.js";
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

/** The part of the official `DLMM` pool object this executor relies on. */
export interface MeteoraPoolClient {
  lbPair: { activeId: number };
  tokenX: { mint: { decimals: number } };
  tokenY: { mint: { decimals: number } };
  getBinArrayForSwap(swapForY: boolean, count?: number): Promise<unknown[]>;
  swapQuote(
    inAmount: unknown,
    swapForY: boolean,
    allowedSlippage: unknown,
    binArrays: unknown[],
  ): QuotedImpact | SwapQuote;
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

const quoteMethods = ["swapQuote", "swapQuoteExactOut", "getBinArrayForSwap", "getPosition"];

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
    const strategy = {
      minBinId: command.min_bin_id,
      maxBinId: command.max_bin_id,
      strategyType:
        command.strategy === "BID_ASK"
          ? this.factory.StrategyType.BidAsk
          : this.factory.StrategyType.Spot,
    };
    const slippage = command.max_slippage_bps / 100;
    const marketBase = {
      activeBinId: pool.lbPair.activeId,
      tokenXDecimals: pool.tokenX.mint.decimals,
      tokenYDecimals: pool.tokenY.mint.decimals,
    };

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
        fromBinId: command.min_bin_id,
        toBinId: command.max_bin_id,
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

export class SolanaRpcTransport implements RpcTransport {
  constructor(
    private readonly connection: Connection,
    private readonly commitment: Commitment = "confirmed",
    private readonly sendOptions: SendOptions = { maxRetries: 0, skipPreflight: true },
  ) {}

  async simulate(command: BuiltCommand): Promise<SimulationTransportResult> {
    const transactions = this.#transactions(command);
    const signers = this.#signers(command);
    const feePayer = signers[0];
    if (feePayer === undefined) {
      throw new Error("wallet signer is unavailable");
    }
    const latest = await this.connection.getLatestBlockhash(this.commitment);
    let computeUnits = 0;
    let networkFeeLamports = 0n;

    for (const transaction of transactions) {
      transaction.recentBlockhash = latest.blockhash;
      transaction.feePayer = feePayer.publicKey;
      // Every signer must be named by the transaction: an OPEN is signed by its new position key.
      transaction.partialSign(...signers);
      const fee = await this.connection.getFeeForMessage(
        transaction.compileMessage(),
        this.commitment,
      );
      networkFeeLamports += BigInt(fee.value ?? 0);
      const result = await this.connection.simulateTransaction(transaction);
      if (result.value.err !== null) {
        throw simulationFailure(result.value.logs ?? []);
      }
      computeUnits += result.value.unitsConsumed ?? 0;
    }

    return {
      computeUnits,
      networkFeeLamports,
      priorityFeeLamports: 0n,
      blockhash: latest.blockhash,
      lastValidBlockHeight: latest.lastValidBlockHeight,
      logs: [],
    };
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
