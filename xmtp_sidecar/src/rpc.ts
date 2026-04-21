/**
 * JSON-RPC 2.0 framing types and write helpers.
 *
 * stdout is the RPC channel — do NOT use console.log here or anywhere that
 * runs on the hot path. All diagnostic output goes to stderr.
 */

export interface JsonRpcRequest {
  jsonrpc: "2.0";
  id: string | number | null;
  method: string;
  params?: Record<string, unknown>;
}

export interface JsonRpcResponse {
  jsonrpc: "2.0";
  id: string | number | null;
  result: unknown;
}

export interface JsonRpcError {
  jsonrpc: "2.0";
  id: string | number | null;
  error: {
    code: number;
    message: string;
  };
}

export interface JsonRpcNotification {
  jsonrpc: "2.0";
  method: string;
  params: Record<string, unknown>;
}

function writeLine(obj: unknown): void {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

export function writeResponse(id: string | number | null, result: unknown): void {
  writeLine({ jsonrpc: "2.0", id, result } satisfies JsonRpcResponse);
}

export function writeError(
  id: string | number | null,
  code: number,
  message: string,
): void {
  writeLine({ jsonrpc: "2.0", id, error: { code, message } } satisfies JsonRpcError);
}

export function writeNotification(
  method: string,
  params: Record<string, unknown>,
): void {
  writeLine({ jsonrpc: "2.0", method, params } satisfies JsonRpcNotification);
}
