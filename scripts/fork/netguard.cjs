// Fork-only network guard, preloaded into the Sherwood CLI by sw.sh (NODE_OPTIONS=--require).
// Every TCP connection the Node process opens is checked at the socket layer (this covers
// fetch/undici, http(s) and ws): loopback is allowed, anything else is refused before DNS
// or connect, so no request leaves the machine. Each attempt is appended to $SW_NETLOG.
// Native addons that do their own networking (the XMTP Rust bindings) are not covered;
// sw.sh refuses the commands that start an XMTP client instead.
'use strict';
const net = require('net');
const dns = require('dns');
const fs = require('fs');

const LOG = process.env.SW_NETLOG;
const LOOPBACK = new Set(['127.0.0.1', 'localhost', '::1', '::ffff:127.0.0.1']);

function log(kind, target, verdict) {
  if (!LOG) return;
  try { fs.appendFileSync(LOG, `${new Date().toISOString()}\t${process.pid}\t${kind}\t${target}\t${verdict}\n`); } catch (_) {}
}

function targetOf(args) {
  let a = args[0];
  if (Array.isArray(a)) a = a[0]; // net internals pass normalized [options, cb]
  if (a && typeof a === 'object') return { host: a.host, port: a.port, path: a.path };
  if (typeof a === 'number' || (typeof a === 'string' && /^\d+$/.test(a))) {
    return { host: typeof args[1] === 'string' ? args[1] : 'localhost', port: a };
  }
  if (typeof a === 'string') return { path: a };
  return {};
}

const origConnect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function (...args) {
  const t = targetOf(args);
  if (t.path) { log('ipc', t.path, 'ALLOW'); return origConnect.apply(this, args); }
  const host = t.host || 'localhost';
  if (LOOPBACK.has(host)) { log('tcp', `${host}:${t.port}`, 'ALLOW'); return origConnect.apply(this, args); }
  log('tcp', `${host}:${t.port}`, 'BLOCK');
  const err = Object.assign(new Error(`netguard: blocked outbound connection to ${host}:${t.port} (fork-only tooling)`), { code: 'ECONNREFUSED' });
  process.nextTick(() => this.destroy(err));
  return this;
};

const origLookup = dns.lookup;
dns.lookup = function (hostname, ...rest) {
  const cb = rest[rest.length - 1];
  if (LOOPBACK.has(hostname)) return origLookup.call(this, hostname, ...rest);
  log('dns', hostname, 'BLOCK');
  const err = Object.assign(new Error(`netguard: blocked DNS lookup of ${hostname}`), { code: 'ENOTFOUND' });
  if (typeof cb === 'function') process.nextTick(() => cb(err));
};
if (dns.promises && dns.promises.lookup) {
  const origP = dns.promises.lookup;
  dns.promises.lookup = async function (hostname, ...rest) {
    if (LOOPBACK.has(hostname)) return origP.call(this, hostname, ...rest);
    log('dns', hostname, 'BLOCK');
    throw Object.assign(new Error(`netguard: blocked DNS lookup of ${hostname}`), { code: 'ENOTFOUND' });
  };
}
