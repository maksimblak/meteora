import Fastify, { type FastifyInstance, type FastifyServerOptions } from "fastify";
import { pathToFileURL } from "node:url";

export function buildServer(options: FastifyServerOptions = {}): FastifyInstance {
  const server = Fastify(options);

  server.get("/health/live", async () => ({ status: "ok" }));
  server.get("/health/ready", async (_request, reply) => {
    return reply.code(503).send({ status: "not_ready" });
  });

  return server;
}

async function start(): Promise<void> {
  const server = buildServer({ logger: true });
  try {
    await server.listen({ host: "0.0.0.0", port: 3000 });
  } catch (error) {
    server.log.error({ error }, "executor startup failed");
    process.exitCode = 1;
  }
}

const entrypoint = process.argv[1];
if (entrypoint !== undefined && import.meta.url === pathToFileURL(entrypoint).href) {
  void start();
}
