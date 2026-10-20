import { z } from "zod";

const base58Alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";

function isPublicKey(value: string): boolean {
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

const PublicKeySchema = z.string().refine(isPublicKey, "invalid Solana public key");
const UtcTimestampSchema = z
  .string()
  .datetime({ offset: true })
  .refine((value) => !Number.isNaN(Date.parse(value)), "invalid timestamp");
const BaseUnitsSchema = z.string().regex(/^(0|[1-9][0-9]*)$/);
const DigestSchema = z.string().regex(/^[a-f0-9]{64}$/);
const NonBlank128Schema = z
  .string()
  .min(1)
  .max(128)
  .refine((value) => value.trim().length > 0, "value must not be blank");
const SignatureSchema = z
  .string()
  .min(1)
  .refine((value) => value.trim().length > 0, "signature must not be blank");

export const DlmmCommandV1Schema = z
  .strictObject({
    schema_version: z.literal(1),
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
    max_slippage_bps: z.number().int().min(0).max(10_000),
    strategy: z.enum(["SPOT", "BID_ASK"]).optional(),
    min_bin_id: z.number().int(),
    max_bin_id: z.number().int(),
    amount_x: BaseUnitsSchema.optional(),
    amount_y: BaseUnitsSchema.optional(),
    remove_bps: z.number().int().min(1).max(10_000).optional(),
  })
  .superRefine((value, context) => {
    if (value.min_bin_id > value.max_bin_id) {
      context.addIssue({ code: "custom", path: ["min_bin_id"], message: "invalid bin range" });
    }
    if (value.operation === "OPEN" || value.operation === "ADD") {
      if (
        value.position_address === undefined ||
        value.strategy === undefined ||
        value.amount_x === undefined ||
        value.amount_y === undefined
      ) {
        context.addIssue({ code: "custom", message: "liquidity fields are required" });
      }
      if (value.remove_bps !== undefined) {
        context.addIssue({ code: "custom", path: ["remove_bps"], message: "unexpected field" });
      }
    } else {
      if (value.position_address === undefined || value.remove_bps === undefined) {
        context.addIssue({ code: "custom", message: "removal fields are required" });
      }
      if (
        value.strategy !== undefined ||
        value.amount_x !== undefined ||
        value.amount_y !== undefined
      ) {
        context.addIssue({ code: "custom", message: "liquidity fields are forbidden" });
      }
      if (value.operation === "CLOSE" && value.remove_bps !== 10_000) {
        context.addIssue({ code: "custom", path: ["remove_bps"], message: "close must remove all" });
      }
    }
  });

export type DlmmCommandV1 = z.infer<typeof DlmmCommandV1Schema>;

export function parseDlmmCommandV1(value: unknown): DlmmCommandV1 {
  return DlmmCommandV1Schema.parse(value);
}

export const SimulationErrorV1Schema = z.strictObject({
  code: z.string().min(1).max(128),
  message: z.string().min(1).max(512),
  retryable: z.boolean(),
});

export const SimulationResultV1Schema = z.strictObject({
  schema_version: z.literal(1),
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
  price_impact_bps: z.number().int().min(0).max(10_000).nullable(),
  min_bin_id: z.number().int(),
  max_bin_id: z.number().int(),
  errors: z.array(SimulationErrorV1Schema),
}).superRefine((value, context) => {
  if (value.success && value.price_impact_bps === null) {
    context.addIssue({
      code: "custom",
      path: ["price_impact_bps"],
      message: "successful simulations require measured price impact",
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

export type SimulationResultV1 = z.infer<typeof SimulationResultV1Schema>;

export const GatewayReceiptV1Schema = z
  .strictObject({
    schema_version: z.literal(1),
    command_id: z.string().uuid(),
    command_digest: DigestSchema,
    simulation_digest: DigestSchema,
    idempotency_key: NonBlank128Schema,
    state: z.enum(["SUBMITTED", "CONFIRMED", "FAILED", "UNKNOWN"]),
    signature: SignatureSchema.nullable(),
    signatures: z.array(SignatureSchema),
    transaction_count: z.number().int().positive(),
    submitted_at: UtcTimestampSchema,
    attempt_count: z.number().int().nonnegative(),
    error: SimulationErrorV1Schema.nullable(),
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

export type GatewayReceiptV1 = z.infer<typeof GatewayReceiptV1Schema>;

export const ChainReceiptV1Schema = z.strictObject({
  schema_version: z.literal(1),
  signature: SignatureSchema,
  state: z.enum(["PENDING", "CONFIRMED", "FAILED", "UNKNOWN"]),
  checked_at: UtcTimestampSchema,
  slot: z.number().int().nonnegative().nullable(),
  error: z.string().nullable(),
});

export type ChainReceiptV1 = z.infer<typeof ChainReceiptV1Schema>;
