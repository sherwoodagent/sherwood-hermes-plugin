/**
 * Active stream management.
 *
 * Uses client.conversations.streamAllMessages() and filters by conversationId,
 * matching the pattern in cli/src/lib/xmtp.ts#streamMessages.
 */

import { ConsentState, type BuiltInContentTypes } from "@xmtp/node-sdk";
import { getClient } from "./client.js";
import { writeNotification } from "./rpc.js";

type Stop = () => void;
const activeStreams = new Map<string, Stop>();

export async function startStream(groupId: string): Promise<string> {
  const client = getClient();

  // Verify the conversation exists before starting the stream
  const conv = await client.conversations.getConversationById(groupId);
  if (!conv) throw new Error(`conversation ${groupId} not found`);

  const streamId = `s_${groupId.slice(0, 8)}_${Date.now()}`;
  let stopped = false;

  (async () => {
    // streamAllMessages returns an AsyncIterable over all groups; we filter by conversationId
    const stream = await client.conversations.streamAllMessages({
      consentStates: [ConsentState.Allowed],
    });
    try {
      for await (const msg of stream) {
        if (stopped) break;
        if (!msg) continue;
        // Only emit messages for the requested group
        if (msg.conversationId !== groupId) continue;

        writeNotification("stream_event", {
          stream_id: streamId,
          group_id: groupId,
          message_id: msg.id ?? "",
          sender_inbox_id: msg.senderInboxId ?? "",
          content:
            typeof msg.content === "string"
              ? msg.content
              : JSON.stringify(msg.content),
          sent_at_ns: String(
            // sentAtNs may be bigint or number depending on SDK version
            (msg as unknown as { sentAtNs?: bigint | number }).sentAtNs ?? 0,
          ),
        });
      }
    } catch (err) {
      console.error(`[xmtp-sidecar] stream ${streamId} error: ${(err as Error).message}`);
    }
  })();

  activeStreams.set(streamId, () => {
    stopped = true;
  });
  return streamId;
}

export function stopStream(streamId: string): boolean {
  const stop = activeStreams.get(streamId);
  if (!stop) return false;
  stop();
  activeStreams.delete(streamId);
  return true;
}

export function stopAll(): void {
  for (const [, stop] of activeStreams) stop();
  activeStreams.clear();
}
