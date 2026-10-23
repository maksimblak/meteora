import {
  QuoteResultV1Schema,
  type QuoteRequestV1,
  type QuoteResultV1,
} from "./contracts.js";
import type { Clock } from "./engine.js";

/** Pool state read with a quote: the LB pair's active bin and the mint decimals. */
export interface PoolState {
  activeBinId: number;
  tokenXDecimals: number;
  tokenYDecimals: number;
}

export interface PoolQuote extends PoolState {
  consumedInAmount: string;
  outAmount: string;
  feeAmount: string;
  priceImpactBps: number;
}

/** Read-only quoting: no wallet, no signature, no transaction. */
export interface PoolQuoter {
  quote(pool: string, swapForY: boolean, inAmount: bigint): Promise<PoolQuote>;
}

export class InsufficientLiquidityError extends Error {
  constructor(public readonly pool: PoolState) {
    super("the pool cannot fill the reference size");
    this.name = "InsufficientLiquidityError";
  }
}

const systemClock: Clock = { now: () => new Date() };

/**
 * `POST /v1/quotes`: price impact and expected output of a reference size by the official
 * SDK quote. A pool too thin for the size is an answer (`INSUFFICIENT_LIQUIDITY`), any
 * other failure is `QUOTE_FAILED`; neither is ever reported as a measured value.
 */
export class QuoteService {
  constructor(
    private readonly quoter: PoolQuoter,
    private readonly clock: Clock = systemClock,
    private readonly timeoutMs = 10_000,
  ) {
    if (timeoutMs <= 0 || !Number.isFinite(timeoutMs)) {
      throw new Error("timeoutMs must be positive");
    }
  }

  async quote(request: QuoteRequestV1): Promise<QuoteResultV1> {
    const base = {
      schema_version: 1 as const,
      pool_address: request.pool_address,
      direction: request.direction,
      in_amount: request.in_amount,
    };
    let quote: PoolQuote;
    try {
      quote = await this.#withTimeout(
        this.quoter.quote(
          request.pool_address,
          request.direction === "X_TO_Y",
          BigInt(request.in_amount),
        ),
      );
    } catch (error) {
      const thin = error instanceof InsufficientLiquidityError;
      return QuoteResultV1Schema.parse({
        ...base,
        quoted_at: this.clock.now().toISOString(),
        success: false,
        consumed_in_amount: null,
        out_amount: null,
        fee_amount: null,
        price_impact_bps: null,
        active_bin_id: thin ? error.pool.activeBinId : null,
        token_x_decimals: thin ? error.pool.tokenXDecimals : null,
        token_y_decimals: thin ? error.pool.tokenYDecimals : null,
        error: thin
          ? {
              code: "INSUFFICIENT_LIQUIDITY",
              message: "the pool cannot fill the reference size",
              retryable: false,
            }
          : { code: "QUOTE_FAILED", message: "the official SDK quote failed", retryable: true },
      });
    }
    const result = QuoteResultV1Schema.safeParse({
      ...base,
      quoted_at: this.clock.now().toISOString(),
      success: true,
      consumed_in_amount: quote.consumedInAmount,
      out_amount: quote.outAmount,
      fee_amount: quote.feeAmount,
      price_impact_bps: quote.priceImpactBps,
      active_bin_id: quote.activeBinId,
      token_x_decimals: quote.tokenXDecimals,
      token_y_decimals: quote.tokenYDecimals,
      error: null,
    });
    if (!result.success) {
      return QuoteResultV1Schema.parse({
        ...base,
        quoted_at: this.clock.now().toISOString(),
        success: false,
        consumed_in_amount: null,
        out_amount: null,
        fee_amount: null,
        price_impact_bps: null,
        active_bin_id: null,
        token_x_decimals: null,
        token_y_decimals: null,
        error: {
          code: "QUOTE_FAILED",
          message: "the official SDK quote is not a measured value",
          retryable: false,
        },
      });
    }
    return result.data;
  }

  async #withTimeout<T>(operation: Promise<T>): Promise<T> {
    let timeout: ReturnType<typeof setTimeout> | undefined;
    try {
      return await Promise.race([
        operation,
        new Promise<T>((_resolve, reject) => {
          timeout = setTimeout(() => reject(new Error("quote timed out")), this.timeoutMs);
        }),
      ]);
    } finally {
      if (timeout !== undefined) {
        clearTimeout(timeout);
      }
    }
  }
}
