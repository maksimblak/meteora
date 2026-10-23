import { z } from "zod";

/**
 * Execution contract V2 (commands, simulations, gateway receipts) and quote contract V1.
 *
 * V2 replaced V1 incompatibly: the executor creates the key of a new position itself, so an
 * OPEN carries no position address and the receipt returns it; an entry carries the LB-pair
 * anchor (active bin, tolerance, token decimals) the executor checks on chain; a simulation
 * returns the observed market facts. A V1 payload is rejected explicitly with
 * `ContractVersionError`, never parsed as a lookalike.
 */

const base58Alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";

export function isPublicKey(value: string): boolean {
  if (value.length === 0 || [...value].some((character) => !base58Alphabet.includes(character))) {
    return false;
  }
  let number = 0n;
  for (const character of value) {
    number = number * 58n + BigInt(base58Alphabet.indexOf(character));
  }
  let bytes = 0;
  for (let remaining = number; remaining > 0n; remaining >>= 8n) {
    bytes += 1;
  }
  const leadingZeroes = value.length - value.replace(/^1+/, "").length;
  return leadingZeroes + bytes === 32;
}

export class ContractVersionError extends Error {
  constructor(
    public readonly received: unknown,
    public readonly supported: number,
  ) {
    super(`contract version ${String(received)} is not supported; expected ${supported}`);
    this.name = "ContractVersionError";
  }
}

const PublicKeySchema = z.string().refine(isPublicKey, "invalid Solana public key");
const UtcTimestampSchema = z
  .string()
  .datetime({ offset: true })
  .refine((value) => !Number.isNaN(Date.parse(value)), "invalid timestamp");
const BaseUnitsSchema = z.string().regex(/^(0|[1-9][0-9]*)$/);
const PositiveBaseUnitsSchema = z.string().regex(/^[1-9][0-9]*$/);
const DigestSchema = z.string().regex(/^[a-f0-9]{64}$/);
const BpsSchema = z.number().int().min(0).max(10_000);
// SPL mint decimals are a u8.
const DecimalsSchema = z.number().int().min(0).max(255);
const NonBlank128Schema = z
  .string()
  .min(1)
  .max(128)
  .refine((value) => value.trim().length > 0, "value must not be blank");
const SignatureSchema = z
  .string()
  .min(1)
  .refine((value) => value.trim().length > 0, "signature must not be blank");

export const ErrorV1Schema = z.strictObject({
  code: z.string().min(1).max(128),
  message: z.string().min(1).max(512),
  retryable: z.boolean(),
});

const anchorFields = [
  "active_bin_id",
  "active_bin_tolerance",
  "token_x_decimals",
  "token_y_decimals",
] as const;

export const DlmmCommandV2Schema = z
  .strictObject({
    schema_version: z.literal(2),
    command_id: z.string().uuid(),
    idempotency_key: NonBlank128Schema,
    correlation_id: z.string().uuid(),
    ruleset_version: NonBlank128Schema,
    mode: z.enum(["SHADOW", "APPROVAL", "LIVE"]),
    operation: z.enum(["OPEN", "ADD", "REMOVE", "CLOSE"]),
    pool_address: PublicKeySchema,
    wallet_address: PublicKeySchema,
    position_address: PublicKeySchema.optional(),
    expires_at: UtcTimestampSchema,
    max_slippage_bps: BpsSchema,
    strategy: z.enum(["SPOT", "BID_ASK"]).optional(),
    min_bin_id: z.number().int(),
    max_bin_id: z.number().int(),
    amount_x: BaseUnitsSchema.optional(),
    amount_y: BaseUnitsSchema.optional(),
    remove_bps: z.number().int().min(1).max(10_000).optional(),
    active_bin_id: z.number().int().optional(),
    active_bin_tolerance: z.number().int().min(0).max(1_000).optional(),
    token_x_decimals: DecimalsSchema.optional(),
    token_y_decimals: DecimalsSchema.optional(),
  })
  .superRefine((value, context) => {
    if (value.min_bin_id > value.max_bin_id) {
      context.addIssue({ code: "custom", path: ["min_bin_id"], message: "invalid bin range" });
    }
    const addsLiquidity = value.operation === "OPEN" || value.operation === "ADD";
    if (value.operation === "OPEN" && value.position_address !== undefined) {
      context.addIssue({
        code: "custom",
        path: ["position_address"],
        message: "the executor creates the position of an OPEN",
      });
    }
    if (value.operation !== "OPEN" && value.position_address === undefined) {
      context.addIssue({ code: "custom", path: ["position_address"], message: "position required" });
    }
    if (addsLiquidity) {
      if (
        value.strategy === undefined ||
        value.amount_x === undefined ||
        value.amount_y === undefined ||
        anchorFields.some((field) => value[field] === undefined)
      ) {
        context.addIssue({ code: "custom", message: "liquidity and anchor fields are required" });
      }
      if (value.remove_bps !== undefined) {
        context.addIssue({ code: "custom", path: ["remove_bps"], message: "unexpected field" });
      }
      return;
    }
    if (value.remove_bps === undefined) {
      context.addIssue({ code: "custom", path: ["remove_bps"], message: "removal requires bps" });
    }
    if (
      value.strategy !== undefined ||
      value.amount_x !== undefined ||
      value.amount_y !== undefined ||
      anchorFields.some((field) => value[field] !== undefined)
    ) {
      context.addIssue({ code: "custom", message: "liquidity and anchor fields are forbidden" });
    }
    if (value.operation === "CLOSE" && value.remove_bps !== 10_000) {
      context.addIssue({ code: "custom", path: ["remove_bps"], message: "close must remove all" });
    }
  });

export type DlmmCommandV2 = z.infer<typeof DlmmCommandV2Schema>;

export const SimulationResultV2Schema = z
  .strictObject({
    schema_version: z.literal(2),
    command_id: z.string().uuid(),
    command_digest: DigestSchema,
    simulation_digest: DigestSchema,
    success: z.boolean(),
    simulated_at: UtcTimestampSchema,
    blockhash: z.string().nullable(),
    last_valid_block_height: z.number().int().nonnegative().nullable(),
    compute_units: z.number().int().nonnegative(),
    network_fee_lamports: BaseUnitsSchema,
    priority_fee_lamports: BaseUnitsSchema,
    price_impact_bps: BpsSchema.nullable(),
    min_bin_id: z.number().int(),
    max_bin_id: z.number().int(),
    position_address: PublicKeySchema.nullable(),
    active_bin_id: z.number().int().nullable(),
    token_x_decimals: DecimalsSchema.nullable(),
    token_y_decimals: DecimalsSchema.nullable(),
    errors: z.array(ErrorV1Schema),
  })
  .superRefine((value, context) => {
    // An exit may succeed with an unknown price impact: it swaps nothing (D08). An entry
    // without a measured impact never succeeds; the engine refuses it.
    if (
      value.success &&
      (value.position_address === null ||
        value.active_bin_id === null ||
        value.token_x_decimals === null ||
        value.token_y_decimals === null)
    ) {
      context.addIssue({
        code: "custom",
        message: "successful simulations require the position and market facts",
      });
    }
    if (!value.success && value.errors.length === 0) {
      context.addIssue({
        code: "custom",
        path: ["errors"],
        message: "failed simulations require an error",
      });
    }
  });

export type SimulationResultV2 = z.infer<typeof SimulationResultV2Schema>;

export const GatewayReceiptV2Schema = z
  .strictObject({
    schema_version: z.literal(2),
    command_id: z.string().uuid(),
    command_digest: DigestSchema,
    simulation_digest: DigestSchema,
    idempotency_key: NonBlank128Schema,
    state: z.enum(["SUBMITTED", "CONFIRMED", "FAILED", "UNKNOWN"]),
    position_address: PublicKeySchema,
    signature: SignatureSchema.nullable(),
    signatures: z.array(SignatureSchema),
    transaction_count: z.number().int().positive(),
    submitted_at: UtcTimestampSchema,
    attempt_count: z.number().int().nonnegative(),
    error: ErrorV1Schema.nullable(),
  })
  .superRefine((value, context) => {
    const primary = value.signatures.at(-1) ?? null;
    if (value.signature !== primary) {
      context.addIssue({
        code: "custom",
        path: ["signature"],
        message: "primary signature must be the final submitted signature",
      });
    }
    if (
      (value.state === "SUBMITTED" || value.state === "CONFIRMED") &&
      value.signatures.length !== value.transaction_count
    ) {
      context.addIssue({
        code: "custom",
        path: ["signatures"],
        message: "submitted receipts require every transaction signature",
      });
    }
    if (value.signatures.length > value.transaction_count) {
      context.addIssue({
        code: "custom",
        path: ["signatures"],
        message: "signature count exceeds transaction count",
      });
    }
  });

export type GatewayReceiptV2 = z.infer<typeof GatewayReceiptV2Schema>;

export const ChainReceiptV1Schema = z.strictObject({
  schema_version: z.literal(1),
  signature: SignatureSchema,
  state: z.enum(["PENDING", "CONFIRMED", "FAILED", "UNKNOWN"]),
  checked_at: UtcTimestampSchema,
  slot: z.number().int().nonnegative().nullable(),
  error: z.string().nullable(),
});

export type ChainReceiptV1 = z.infer<typeof ChainReceiptV1Schema>;

/** Read-only quote of a reference swap size against one pool (no wallet, no signature). */
export const QuoteRequestV1Schema = z.strictObject({
  schema_version: z.literal(1),
  pool_address: PublicKeySchema,
  direction: z.enum(["X_TO_Y", "Y_TO_X"]),
  in_amount: PositiveBaseUnitsSchema,
});

export type QuoteRequestV1 = z.infer<typeof QuoteRequestV1Schema>;

const quoteAmounts = ["consumed_in_amount", "out_amount", "fee_amount", "price_impact_bps"] as const;

export const QuoteResultV1Schema = z
  .strictObject({
    schema_version: z.literal(1),
    pool_address: PublicKeySchema,
    direction: z.enum(["X_TO_Y", "Y_TO_X"]),
    in_amount: PositiveBaseUnitsSchema,
    quoted_at: UtcTimestampSchema,
    success: z.boolean(),
    consumed_in_amount: BaseUnitsSchema.nullable(),
    out_amount: BaseUnitsSchema.nullable(),
    fee_amount: BaseUnitsSchema.nullable(),
    price_impact_bps: BpsSchema.nullable(),
    active_bin_id: z.number().int().nullable(),
    token_x_decimals: DecimalsSchema.nullable(),
    token_y_decimals: DecimalsSchema.nullable(),
    error: ErrorV1Schema.nullable(),
  })
  .superRefine((value, context) => {
    if (
      value.success &&
      (value.error !== null ||
        quoteAmounts.some((field) => value[field] === null) ||
        value.active_bin_id === null ||
        value.token_x_decimals === null ||
        value.token_y_decimals === null)
    ) {
      context.addIssue({
        code: "custom",
        message: "a successful quote carries every measured value and no error",
      });
    }
    if (!value.success && value.error === null) {
      context.addIssue({ code: "custom", path: ["error"], message: "a failed quote needs an error" });
    }
  });

export type QuoteResultV1 = z.infer<typeof QuoteResultV1Schema>;

function parseVersioned<T>(schema: z.ZodType<T>, version: number, value: unknown): T {
  if (value !== null && typeof value === "object" && "schema_version" in value) {
    const received = (value as { schema_version: unknown }).schema_version;
    if (received !== version) {
      throw new ContractVersionError(received, version);
    }
  }
  return schema.parse(value);
}

export function parseDlmmCommandV2(value: unknown): DlmmCommandV2 {
  return parseVersioned(DlmmCommandV2Schema, 2, value);
}

export function parseSimulationResultV2(value: unknown): SimulationResultV2 {
  return parseVersioned(SimulationResultV2Schema, 2, value);
}

export function parseGatewayReceiptV2(value: unknown): GatewayReceiptV2 {
  return parseVersioned(GatewayReceiptV2Schema, 2, value);
}

export function parseQuoteRequestV1(value: unknown): QuoteRequestV1 {
  return parseVersioned(QuoteRequestV1Schema, 1, value);
}
