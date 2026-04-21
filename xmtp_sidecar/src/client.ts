/**
 * XMTP Client singleton.
 *
 * Lazy-initialized via the create_client RPC method.
 * Signer shape is derived from the installed @xmtp/node-sdk v6 API
 * (matches cli/src/lib/xmtp.ts usage).
 */

import { Client, IdentifierKind, type BuiltInContentTypes, type ClientOptions, type Signer, type XmtpEnv } from "@xmtp/node-sdk";
import { privateKeyToAccount } from "viem/accounts";
import { keccak256, toBytes } from "viem";

// Use the widened type to accommodate Client.create's return without extra codecs
let _client: Client<BuiltInContentTypes> | null = null;
let _signerAddress: string | null = null;

function deriveDbEncryptionKey(privateKeyHex: string): Uint8Array {
  // Mirror the CLI derivation: keccak256(privateKey + "xmtp-db-key")
  const hash = keccak256(toBytes(privateKeyHex + "xmtp-db-key"));
  return toBytes(hash);
}

export async function createClient(
  privateKeyHex: string,
  dbPath: string,
  env: "production" | "dev",
): Promise<{ address: string; inbox_id: string }> {
  if (_client) {
    return { address: _signerAddress!, inbox_id: _client.inboxId };
  }

  const account = privateKeyToAccount(privateKeyHex as `0x${string}`);

  const signer: Signer = {
    type: "EOA",
    getIdentifier: () => ({
      identifier: account.address.toLowerCase(),
      identifierKind: IdentifierKind.Ethereum,
    }),
    signMessage: async (message: string) => {
      const signature = await account.signMessage({ message });
      return toBytes(signature);
    },
  };

  // ClientOptions is a discriminated union: (NetworkOptions | {backend}) & StorageOptions & ...
  // Construct the network part separately so TypeScript can resolve the union branch,
  // then spread storage options alongside it.
  const networkPart: ClientOptions = { env: env as XmtpEnv };
  _client = (await Client.create(signer, {
    ...networkPart,
    dbEncryptionKey: deriveDbEncryptionKey(privateKeyHex),
    dbPath,
  } as ClientOptions)) as Client<BuiltInContentTypes>;

  _signerAddress = account.address;
  return { address: account.address, inbox_id: _client.inboxId };
}

export function getClient(): Client<BuiltInContentTypes> {
  if (!_client) throw new Error("XMTP client not initialized; call create_client first");
  return _client;
}
