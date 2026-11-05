/**
 * ENS subdomain resolver for sherwoodagent.eth.
 *
 * Sherwood uses a Durin L2Registry on Base (not standard mainnet ENS).
 * Text records are stored under the L2Registry contract on Base mainnet
 * (address 0x7a019ce699e27b0ad1e5b51344a58116b9f3b9b1).
 *
 * Older Sherwood CLI releases stored the XMTP group ID there under key
 * "xmtpGroupId". Current releases write no ENS record (Robinhood Chain has no
 * registry), so funds they create do not resolve here and get no XMTP stream;
 * their on-chain events still reach the plugin through `sherwood session check`.
 *
 * ENV knobs:
 *   SIDECAR_L2_REGISTRY   — override the registry address
 *   SIDECAR_BASE_RPC      — override the Base RPC URL (default https://mainnet.base.org)
 */

import { createPublicClient, http } from "viem";
import { namehash } from "viem/ens";
import { base } from "viem/chains";

const ENS_DOMAIN = "sherwoodagent.eth";
const ENS_TEXT_KEY = "xmtpGroupId";

// Default: Base mainnet L2Registry (Durin)
const DEFAULT_L2_REGISTRY = "0x7a019ce699e27b0ad1e5b51344a58116b9f3b9b1" as const;
const DEFAULT_BASE_RPC = "https://mainnet.base.org";

const L2_REGISTRY_ABI = [
  {
    name: "text",
    type: "function",
    stateMutability: "view",
    inputs: [
      { name: "node", type: "bytes32" },
      { name: "key", type: "string" },
    ],
    outputs: [{ name: "", type: "string" }],
  },
] as const;

function getRegistryAddress(): `0x${string}` {
  return (process.env["SIDECAR_L2_REGISTRY"] ?? DEFAULT_L2_REGISTRY) as `0x${string}`;
}

function getBaseRpc(): string {
  return process.env["SIDECAR_BASE_RPC"] ?? DEFAULT_BASE_RPC;
}

function getBaseClient() {
  return createPublicClient({
    chain: base,
    transport: http(getBaseRpc()),
  });
}

/**
 * Resolve a fund subdomain to its XMTP group ID.
 * Reads the "xmtpGroupId" text record from the Durin L2Registry on Base.
 */
export async function resolveSubdomainToGroupId(subdomain: string): Promise<string> {
  const fullName = `${subdomain}.${ENS_DOMAIN}`;
  const node = namehash(fullName);
  const client = getBaseClient();

  const text = await client.readContract({
    address: getRegistryAddress(),
    abi: L2_REGISTRY_ABI,
    functionName: "text",
    args: [node, ENS_TEXT_KEY],
  });

  if (!text) throw new Error(`no XMTP group found for ${fullName}`);
  return text as string;
}
