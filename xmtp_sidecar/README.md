# xmtp-sidecar

A self-contained JSON-RPC 2.0 server over stdin/stdout that wraps `@xmtp/node-sdk`.

## Why it exists

The Sherwood CLI's global npm install fails to load `@xmtp/node-bindings` on Debian 12 (glibc < 2.38). By being the **root** of its own install tree, this sub-project's `package.json#overrides` correctly pins the compatible pre-built binding (`1.10.0-dev.97e86c6`) without interference from any parent workspace.

## Usage

### Build

```bash
cd hermes-plugin/xmtp_sidecar
npm ci
npm run build
```

### Run

```bash
node dist/index.js
```

Or after linking the bin:

```bash
xmtp-sidecar
```

The process reads newline-delimited JSON-RPC 2.0 from **stdin**, writes responses and notifications to **stdout**, and sends diagnostics to **stderr**.

### How the Python plugin invokes it

```python
import subprocess, json

proc = subprocess.Popen(
    ["node", "/path/to/xmtp_sidecar/dist/index.js"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    text=True,
)

def rpc(method, params, req_id=1):
    req = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
    proc.stdin.write(req + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())
```

## JSON-RPC Methods

| Method | Params | Returns |
|--------|--------|---------|
| `ping` | `{}` | `{ok: true}` |
| `create_client` | `{private_key_hex, db_path, env}` | `{address, inbox_id}` |
| `resolve_subdomain` | `{subdomain}` | `{group_id}` |
| `get_conversation` | `{group_id}` | `{member_of}` |
| `send_text` | `{group_id, text, markdown?}` | `{message_id}` |
| `stream_start` | `{group_id}` | `{stream_id}` |
| `stream_stop` | `{stream_id}` | `{stopped}` |
| `shutdown` | `{}` | `{ok: true}` then exits |

### `stream_event` notification (sidecar to Python, unsolicited)

```json
{
  "jsonrpc": "2.0",
  "method": "stream_event",
  "params": {
    "stream_id": "s_abc",
    "group_id": "0x...",
    "message_id": "msg_xyz",
    "sender_inbox_id": "...",
    "content": "text",
    "sent_at_ns": "1710950412345000000"
  }
}
```

## ENS Resolution

`resolve_subdomain` reads the `xmtpGroupId` text record from the Durin L2Registry on Base mainnet. The key matches what the Sherwood CLI writes when `sherwood chat <name> init` is called.

### ENV knobs

| Variable | Default | Description |
|----------|---------|-------------|
| `SIDECAR_L2_REGISTRY` | `0x7a019ce699e27b0ad1e5b51344a58116b9f3b9b1` | L2Registry address (override for Base Sepolia: `0x06eb7b85b59bc3e50fe4837be776cdd26de602cf`) |
| `SIDECAR_BASE_RPC` | `https://mainnet.base.org` | Base RPC URL |

## Pinned binding

After `npm ci`, verify the override was honored:

```bash
cat node_modules/@xmtp/node-bindings/package.json | grep '"version"'
# must be: "version": "1.10.0-dev.97e86c6"
```

If the version differs, the override did not apply — check that you are running `npm ci` from inside `xmtp_sidecar/` (not from a parent workspace root).
