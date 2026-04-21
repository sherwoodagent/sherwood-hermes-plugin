#!/usr/bin/env node
/**
 * XMTP JSON-RPC sidecar entry point.
 *
 * Reads line-delimited JSON-RPC 2.0 requests from stdin.
 * Writes responses and notifications to stdout.
 * All diagnostic output goes to stderr.
 */

import readline from "node:readline";
import { dispatch } from "./methods.js";
import { writeResponse, writeError } from "./rpc.js";
import { stopAll } from "./streams.js";

const rl = readline.createInterface({ input: process.stdin, terminal: false });

rl.on("line", async (line) => {
  if (!line.trim()) return;
  let msg: { id?: unknown; method?: unknown; params?: unknown };
  try {
    msg = JSON.parse(line) as typeof msg;
  } catch {
    console.error(`[xmtp-sidecar] malformed json: ${line.slice(0, 200)}`);
    return;
  }

  const id = msg.id as string | number | null | undefined;
  const method = typeof msg.method === "string" ? msg.method : undefined;
  const params =
    msg.params && typeof msg.params === "object" && !Array.isArray(msg.params)
      ? (msg.params as Record<string, unknown>)
      : {};

  if (!method) {
    if (id !== undefined && id !== null) {
      writeError(id, -32600, "invalid request: missing method");
    } else {
      console.error("[xmtp-sidecar] invalid request: missing method");
    }
    return;
  }

  try {
    const result = await dispatch(method, params);
    if (id !== undefined) writeResponse(id, result);
  } catch (err) {
    const message = (err as Error).message ?? "unknown error";
    if (id !== undefined && id !== null) {
      // Preserve JSON-RPC error codes if the handler attached one
      const code = (err as { code?: number }).code ?? -32000;
      writeError(id, code, message);
    } else {
      console.error(`[xmtp-sidecar] notification error: ${message}`);
    }
  }
});

rl.on("close", () => {
  stopAll();
  process.exit(0);
});

// Graceful shutdown
process.on("SIGTERM", () => {
  stopAll();
  process.exit(0);
});
process.on("SIGINT", () => {
  stopAll();
  process.exit(0);
});

console.error("[xmtp-sidecar] ready");
