/**
 * JSON-RPC method handlers.
 *
 * dispatch() is called by index.ts for every incoming request.
 * Throws on error; caller formats the JSON-RPC error response.
 */

import { ConsentState } from "@xmtp/node-sdk";
import { createClient, getClient } from "./client.js";
import { resolveSubdomainToGroupId } from "./ens.js";
import { startStream, stopStream, stopAll } from "./streams.js";

type Params = Record<string, unknown>;

function str(params: Params, key: string): string {
  const v = params[key];
  if (typeof v !== "string") throw new Error(`missing or non-string param: ${key}`);
  return v;
}

function optStr(params: Params, key: string): string | undefined {
  const v = params[key];
  return typeof v === "string" ? v : undefined;
}

// ── Method handlers ──

async function ping(_params: Params): Promise<{ ok: true }> {
  return { ok: true };
}

async function handle_create_client(params: Params): Promise<{ address: string; inbox_id: string }> {
  const privateKeyHex = str(params, "private_key_hex");
  const dbPath = str(params, "db_path");
  const env = str(params, "env") as "production" | "dev";
  if (env !== "production" && env !== "dev") {
    throw new Error(`env must be "production" or "dev", got: ${env}`);
  }
  return createClient(privateKeyHex, dbPath, env);
}

async function resolve_subdomain(params: Params): Promise<{ group_id: string }> {
  const subdomain = str(params, "subdomain");
  const groupId = await resolveSubdomainToGroupId(subdomain);
  return { group_id: groupId };
}

async function get_conversation(params: Params): Promise<{ member_of: boolean }> {
  const client = getClient();
  const groupId = str(params, "group_id");

  // Sync to ensure we have the latest conversation list
  await client.conversations.syncAll([ConsentState.Allowed]);
  const conv = await client.conversations.getConversationById(groupId);
  return { member_of: !!conv };
}

async function send_text(params: Params): Promise<{ message_id: string }> {
  const client = getClient();
  const groupId = str(params, "group_id");
  const text = str(params, "text");
  // markdown flag is accepted but ignored — content type is plain text at the transport layer
  const _markdown = optStr(params, "markdown");

  await client.conversations.syncAll([ConsentState.Allowed]);
  const conv = await client.conversations.getConversationById(groupId);
  if (!conv) throw new Error(`conversation ${groupId} not found`);

  const messageId = await conv.sendText(text);
  return { message_id: String(messageId ?? "") };
}

async function stream_start(params: Params): Promise<{ stream_id: string }> {
  const groupId = str(params, "group_id");
  const streamId = await startStream(groupId);
  return { stream_id: streamId };
}

async function stream_stop(params: Params): Promise<{ stopped: boolean }> {
  const streamId = str(params, "stream_id");
  const stopped = stopStream(streamId);
  return { stopped };
}

async function shutdown(_params: Params): Promise<{ ok: true }> {
  stopAll();
  // Defer exit so the response can be flushed
  setTimeout(() => process.exit(0), 10);
  return { ok: true };
}

// ── Dispatch table ──

const HANDLERS: Record<string, (params: Params) => Promise<unknown>> = {
  ping,
  create_client: handle_create_client,
  resolve_subdomain,
  get_conversation,
  send_text,
  stream_start,
  stream_stop,
  shutdown,
};

export async function dispatch(method: string, params: Params): Promise<unknown> {
  const handler = HANDLERS[method];
  if (!handler) {
    throw Object.assign(new Error(`method not found: ${method}`), { code: -32601 });
  }
  return handler(params);
}
