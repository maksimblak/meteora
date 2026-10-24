import Fastify, {
  type FastifyInstance,
  type FastifyReply,
  type FastifyServerOptions,
} from "fastify";
import { Connection } from "@solana/web3.js";
import { timingSafeEqual } from "node:crypto";
import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";

import { ZodError } from "zod";

import { ContractVersionError, parseDlmmCommandV2, parseQuoteRequestV1 } from "./contracts.js";
import {
  ExecutionEngine,
  FileExecutionStateStore,
  GatewayError,
  HmacApprovalProofVerifier,
  type LivePromotionEvidence,
  type TradingMode,
} from "./engine.js";
import { QuoteService } from "./quotes.js";
import {
  FileSecretSignerProvider,
  OfficialMeteoraDlmmBoundary,
  OfficialMeteoraQuoter,
  SolanaLiveReadiness,
  SolanaRpcTransport,
  priorityFeePolicyFromEnvironment,
  type LiveReadiness,
  type LiveReadinessProbe,
} from "./sdk.js";

export interface ExecutorServerConfig {
  engine?: ExecutionEngine;
  /** Read-only quotes; needs an RPC but never a wallet, so SHADOW can serve them. */
  quotes?: QuoteService;
  internalToken?: string;
  modeToken?: string;
  configuredMode?: TradingMode;
  /** Wallet, RPC and price impact measurement, re-checked on every readiness request. */
  liveReadiness?: LiveReadinessProbe;
}

export async function buildRuntimeConfig(
  environ: NodeJS.ProcessEnv = process.env,
): Promise<ExecutorServerConfig> {
  const configuredMode = parseMode(environ.TRADING_MODE);
  const internalToken = await readOptionalSecret(
    environ.EXECUTOR_INTERNAL_TOKEN,
    environ.EXECUTOR_INTERNAL_TOKEN_FILE,
  );
  const modeToken = await readOptionalSecret(
    environ.EXECUTOR_MODE_TOKEN,
    environ.EXECUTOR_MODE_TOKEN_FILE,
  );
  const approvalSecret = await readOptionalSecret(
    environ.EXECUTOR_APPROVAL_SECRET,
    environ.EXECUTOR_APPROVAL_SECRET_FILE,
  );
  const connection = openConnection(environ.SOLANA_RPC_URL);
  const base: ExecutorServerConfig = {
    configuredMode,
    ...(internalToken === undefined ? {} : { internalToken }),
    ...(modeToken === undefined ? {} : { modeToken }),
    ...(connection === undefined || internalToken === undefined
      ? {}
      : { quotes: new QuoteService(new OfficialMeteoraQuoter(connection)) }),
  };
  if (configuredMode === "SHADOW") {
    return base;
  }

  const walletPath = environ.WALLET_KEYPAIR_FILE;
  const statePath = environ.EXECUTOR_STATE_FILE;
  const livePromotion =
    configuredMode === "LIVE"
      ? await readLivePromotion(environ.LIVE_PROMOTION_FILE)
      : undefined;
  if (
    connection === undefined ||
    walletPath === undefined ||
    statePath === undefined ||
    internalToken === undefined ||
    modeToken === undefined ||
    (configuredMode === "APPROVAL" && approvalSecret === undefined) ||
    (configuredMode === "LIVE" && livePromotion === undefined)
  ) {
    return base;
  }
  try {
    // A misread fee ceiling is a misconfiguration: no money engine, readiness stays false.
    const priorityFees = priorityFeePolicyFromEnvironment(environ);
    const signers = new FileSecretSignerProvider(walletPath);
    const wallet = await signers.loadWalletSigner();
    if (
      environ.WALLET_PUBLIC_KEY !== undefined &&
      wallet.publicKey.toBase58() !== environ.WALLET_PUBLIC_KEY
    ) {
      return base;
    }
    const engine = new ExecutionEngine({
      sdk: new OfficialMeteoraDlmmBoundary(connection, signers),
      rpc: new SolanaRpcTransport(connection, "confirmed", undefined, priorityFees),
      store: new FileExecutionStateStore(statePath),
      submissionEnabled: true,
      allowedMode: configuredMode,
      ...(approvalSecret === undefined
        ? {}
        : { approvalProofVerifier: new HmacApprovalProofVerifier(approvalSecret) }),
      ...(livePromotion === undefined ? {} : { livePromotion }),
    });
    const liveReadiness = new SolanaLiveReadiness(
      connection,
      signers,
      wallet.publicKey.toBase58(),
    );
    return { ...base, engine, liveReadiness };
  } catch {
    return base;
  }
}

function tokenMatches(expected: string | undefined, actual: string | undefined): boolean {
  if (expected === undefined || actual === undefined) {
    return false;
  }
  const left = Buffer.from(expected);
  const right = Buffer.from(actual);
  return left.length === right.length && timingSafeEqual(left, right);
}

const unavailable: LiveReadiness = { wallet: false, rpc: false, priceImpact: false };

async function missingForMoney(config: ExecutorServerConfig): Promise<string[]> {
  if (
    config.engine === undefined ||
    config.internalToken === undefined ||
    config.modeToken === undefined ||
    config.liveReadiness === undefined
  ) {
    return ["configuration"];
  }
  let state: LiveReadiness;
  try {
    state = await config.liveReadiness.check();
  } catch {
    state = unavailable;
  }
  return [
    ...(state.wallet ? [] : ["wallet"]),
    ...(state.rpc ? [] : ["rpc"]),
    ...(state.priceImpact ? [] : ["price_impact"]),
  ];
}

export function buildServer(
  options: FastifyServerOptions = {},
  config: ExecutorServerConfig = {},
): FastifyInstance {
  const server = Fastify(options);

  server.get("/health/live", async () => ({ status: "ok" }));
  server.get("/health/ready", async (_request, reply) => {
    if (config.configuredMode === "SHADOW") {
      return reply.code(200).send({ status: "ready", mode: "SHADOW" });
    }
    if (config.configuredMode !== "APPROVAL" && config.configuredMode !== "LIVE") {
      return reply.code(503).send({ status: "not_ready", missing: ["configuration"] });
    }
    // Names of what is missing only - never a value, a path or a key.
    const missing = await missingForMoney(config);
    if (missing.length === 0) {
      return reply.code(200).send({ status: "ready", mode: config.configuredMode });
    }
    return reply.code(503).send({ status: "not_ready", missing });
  });

  server.post("/v1/quotes", async (request, reply) => {
    if (!tokenMatches(config.internalToken, request.headers["x-internal-token"] as string | undefined)) {
      return reply.code(401).send({ error: "unauthorized" });
    }
    if (config.quotes === undefined) {
      return reply.code(503).send({ error: "quotes_unavailable" });
    }
    try {
      return reply.code(200).send(await config.quotes.quote(parseQuoteRequestV1(request.body)));
    } catch (error) {
      return sendKnownError(reply, error);
    }
  });

  server.post("/v1/simulations", async (request, reply) => {
    if (!tokenMatches(config.internalToken, request.headers["x-internal-token"] as string | undefined)) {
      return reply.code(401).send({ error: "unauthorized" });
    }
    let command;
    try {
      command = parseDlmmCommandV2(request.body);
    } catch (error) {
      return sendKnownError(reply, error);
    }
    if (config.engine === undefined) {
      return reply.code(503).send({ error: "execution_unavailable" });
    }
    try {
      return reply.code(200).send(await config.engine.simulate(command));
    } catch (error) {
      return sendKnownError(reply, error);
    }
  });

  server.post("/v1/commands", async (request, reply) => {
    if (!tokenMatches(config.internalToken, request.headers["x-internal-token"] as string | undefined)) {
      return reply.code(401).send({ error: "unauthorized" });
    }
    let command;
    try {
      command = parseDlmmCommandV2(request.body);
    } catch (error) {
      return sendKnownError(reply, error);
    }
    if (config.engine === undefined) {
      return reply.code(503).send({ error: "execution_unavailable" });
    }
    const idempotencyKey = request.headers["idempotency-key"] as string | undefined;
    if (idempotencyKey === undefined || idempotencyKey.length === 0) {
      return reply.code(400).send({ error: "idempotency_key_required" });
    }
    try {
      const modeTokenVerified = tokenMatches(
        config.modeToken,
        request.headers["x-mode-token"] as string | undefined,
      );
      const receipt = await config.engine.submit(command, idempotencyKey, {
        modeTokenVerified,
        ...(typeof request.headers["x-approval-id"] === "string"
          ? { approvalId: request.headers["x-approval-id"] }
          : {}),
        ...(typeof request.headers["x-approval-approved-at"] === "string"
          ? { approvalApprovedAt: request.headers["x-approval-approved-at"] }
          : {}),
        ...(typeof request.headers["x-approval-expires-at"] === "string"
          ? { approvalExpiresAt: request.headers["x-approval-expires-at"] }
          : {}),
        ...(typeof request.headers["x-operator-id"] === "string"
          ? { operatorId: request.headers["x-operator-id"] }
          : {}),
        ...(typeof request.headers["x-command-digest"] === "string"
          ? { commandDigest: request.headers["x-command-digest"] }
          : {}),
        ...(typeof request.headers["x-simulation-digest"] === "string"
          ? { simulationDigest: request.headers["x-simulation-digest"] }
          : {}),
        ...(typeof request.headers["x-approval-proof"] === "string"
          ? { approvalProof: request.headers["x-approval-proof"] }
          : {}),
      });
      return reply.code(receipt.state === "SUBMITTED" ? 202 : 200).send(receipt);
    } catch (error) {
      return sendKnownError(reply, error);
    }
  });

  server.get<{ Params: { id: string } }>("/v1/commands/:id", async (request, reply) => {
    if (!tokenMatches(config.internalToken, request.headers["x-internal-token"] as string | undefined)) {
      return reply.code(401).send({ error: "unauthorized" });
    }
    const receipt = await config.engine?.get(request.params.id);
    return receipt === undefined
      ? reply.code(404).send({ error: "command_not_found" })
      : reply.code(200).send(receipt);
  });

  return server;
}

function sendKnownError(
  reply: FastifyReply,
  error: unknown,
): unknown {
  if (error instanceof ContractVersionError) {
    return reply.code(400).send({ error: "unsupported_contract_version" });
  }
  if (error instanceof ZodError) {
    return reply.code(400).send({ error: "invalid_contract" });
  }
  if (error instanceof GatewayError) {
    return reply.code(error.statusCode).send({ error: error.code.toLowerCase() });
  }
  return reply.code(500).send({ error: "executor_error" });
}

async function start(): Promise<void> {
  const server = buildServer({ logger: true }, await buildRuntimeConfig());
  try {
    await server.listen({ host: "0.0.0.0", port: 3000 });
  } catch (error) {
    server.log.error({ error }, "executor startup failed");
    process.exitCode = 1;
  }
}

function parseMode(value: string | undefined): TradingMode {
  return value === "APPROVAL" || value === "LIVE" ? value : "SHADOW";
}

function openConnection(url: string | undefined): Connection | undefined {
  if (url === undefined || url.trim().length === 0) {
    return undefined;
  }
  try {
    return new Connection(url, "confirmed");
  } catch {
    return undefined;
  }
}

async function readOptionalSecret(
  direct: string | undefined,
  path: string | undefined,
): Promise<string | undefined> {
  if (direct !== undefined && direct.length > 0) {
    return direct;
  }
  if (path === undefined) {
    return undefined;
  }
  try {
    const value = (await readFile(path, "utf8")).trim();
    return value.length === 0 ? undefined : value;
  } catch {
    return undefined;
  }
}

async function readLivePromotion(
  path: string | undefined,
): Promise<LivePromotionEvidence | undefined> {
  if (path === undefined) {
    return undefined;
  }
  try {
    const value = JSON.parse(await readFile(path, "utf8")) as Partial<LivePromotionEvidence>;
    if (
      typeof value.promotionId !== "string" ||
      value.promotionId.trim().length === 0 ||
      typeof value.rulesetVersion !== "string" ||
      !Number.isInteger(value.approvalCycles) ||
      (value.approvalCycles ?? 0) < 10 ||
      value.receiptsReconciled !== true ||
      value.riskInvariantsPreserved !== true ||
      value.dedicatedRpcReady !== true ||
      value.walletReady !== true ||
      value.checklistCompleted !== true ||
      typeof value.operatorConfirmationId !== "string" ||
      value.operatorConfirmationId.trim().length === 0 ||
      typeof value.promotedAt !== "string" ||
      !Number.isFinite(Date.parse(value.promotedAt)) ||
      Date.parse(value.promotedAt) > Date.now() ||
      typeof value.expiresAt !== "string" ||
      !Number.isFinite(Date.parse(value.expiresAt)) ||
      Date.parse(value.expiresAt) <= Date.parse(value.promotedAt) ||
      Date.parse(value.expiresAt) <= Date.now()
    ) {
      return undefined;
    }
    return value as LivePromotionEvidence;
  } catch {
    return undefined;
  }
}

const entrypoint = process.argv[1];
if (entrypoint !== undefined && import.meta.url === pathToFileURL(entrypoint).href) {
  void start();
}
