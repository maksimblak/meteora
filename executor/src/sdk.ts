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
import { join } from "node:path";

import type { DlmmCommandV1 } from "./contracts.js";
import type {
  BuiltCommand,
  RpcStatus,
  RpcTransport,
  SignatureRecorder,
  SdkBoundary,
  SimulationTransportResult,
} from "./engine.js";

const base58Alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";

export interface MeteoraPoolClient {
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
const MeteoraDlmm = require("@meteora-ag/dlmm") as MeteoraDlmmFactory;
const BN = require("bn.js") as BigNumberConstructor;

export interface SecretSignerProvider {
  loadWalletSigner(): Promise<Keypair>;
  loadPositionSigner(positionAddress: string): Promise<Keypair>;
}

export class FileSecretSignerProvider implements SecretSignerProvider {
  constructor(
    private readonly walletSecretPath: string,
    private readonly positionSecretDirectory: string,
  ) {}

  async loadWalletSigner(): Promise<Keypair> {
    return this.#load(this.walletSecretPath, undefined);
  }

  async loadPositionSigner(positionAddress: string): Promise<Keypair> {
    new PublicKey(positionAddress);
    return this.#load(join(this.positionSecretDirectory, `${positionAddress}.json`), positionAddress);
  }

  async #load(path: string, expectedAddress: string | undefined): Promise<Keypair> {
    try {
      const parsed: unknown = JSON.parse(await readFile(path, "utf8"));
      if (
        !Array.isArray(parsed) ||
        parsed.length !== 64 ||
        parsed.some((value) => !Number.isInteger(value) || value < 0 || value > 255)
      ) {
        throw new Error("invalid secret material");
      }
      const keypair = Keypair.fromSecretKey(Uint8Array.from(parsed as number[]));
      if (expectedAddress !== undefined && keypair.publicKey.toBase58() !== expectedAddress) {
        throw new Error("signer address mismatch");
      }
      return keypair;
    } catch {
      throw new Error("secret signer is unavailable or invalid");
    }
  }
}

export class OfficialMeteoraDlmmBoundary implements SdkBoundary {
  constructor(
    private readonly connection: Connection,
    private readonly signers: SecretSignerProvider,
    private readonly factory: MeteoraDlmmFactory = MeteoraDlmm,
  ) {}

  async build(command: DlmmCommandV1): Promise<BuiltCommand> {
    const wallet = await this.signers.loadWalletSigner();
    if (wallet.publicKey.toBase58() !== command.wallet_address) {
      throw new Error("configured wallet does not match command wallet");
    }
    const pool = await this.factory.create(this.connection, new PublicKey(command.pool_address));
    const positionAddress = command.position_address;
    if (positionAddress === undefined) {
      throw new Error("position address is required");
    }
    const position = new PublicKey(positionAddress);
    const strategy = {
      minBinId: command.min_bin_id,
      maxBinId: command.max_bin_id,
      strategyType:
        command.strategy === "BID_ASK"
          ? this.factory.StrategyType.BidAsk
          : this.factory.StrategyType.Spot,
    };
    const slippage = command.max_slippage_bps / 100;
    let transactions: Transaction[];
    const extraSigners: Keypair[] = [];

    if (command.operation === "OPEN") {
      const positionSigner = await this.signers.loadPositionSigner(positionAddress);
      extraSigners.push(positionSigner);
      const transaction = await pool.initializePositionAndAddLiquidityByStrategy({
        positionPubKey: position,
        totalXAmount: new BN(command.amount_x ?? "0"),
        totalYAmount: new BN(command.amount_y ?? "0"),
        strategy,
        user: wallet.publicKey,
        slippage,
      });
      transactions = [transaction];
    } else if (command.operation === "ADD") {
      const transaction = await pool.addLiquidityByStrategy({
        positionPubKey: position,
        totalXAmount: new BN(command.amount_x ?? "0"),
        totalYAmount: new BN(command.amount_y ?? "0"),
        strategy,
        user: wallet.publicKey,
        slippage,
      });
      transactions = [transaction];
    } else {
      transactions = await pool.removeLiquidity({
        user: wallet.publicKey,
        position,
        fromBinId: command.min_bin_id,
        toBinId: command.max_bin_id,
        bps: new BN(command.remove_bps ?? 0),
        shouldClaimAndClose: command.operation === "CLOSE",
      });
    }

    return {
      command,
      opaqueTransactions: transactions,
      opaqueSigners: [wallet, ...extraSigners],
    };
  }
}

export class SolanaRpcTransport implements RpcTransport {
  constructor(
    private readonly connection: Connection,
    private readonly commitment: Commitment = "confirmed",
    private readonly sendOptions: SendOptions = { maxRetries: 0, skipPreflight: true },
  ) {}

  async simulate(command: BuiltCommand): Promise<SimulationTransportResult> {
    if (
      command.priceImpactBps === undefined ||
      !Number.isInteger(command.priceImpactBps) ||
      !Number.isFinite(command.priceImpactBps) ||
      command.priceImpactBps < 0 ||
      command.priceImpactBps > 10_000
    ) {
      throw new Error("measured price impact is unavailable");
    }
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
      transaction.partialSign(...signers);
      const fee = await this.connection.getFeeForMessage(
        transaction.compileMessage(),
        this.commitment,
      );
      networkFeeLamports += BigInt(fee.value ?? 0);
      const result = await this.connection.simulateTransaction(transaction);
      if (result.value.err !== null) {
        throw new Error("transaction simulation failed");
      }
      computeUnits += result.value.unitsConsumed ?? 0;
    }

    return {
      computeUnits,
      networkFeeLamports,
      priorityFeeLamports: 0n,
      priceImpactBps: command.priceImpactBps,
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
