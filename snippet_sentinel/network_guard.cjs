const dns = require('node:dns');
const net = require('node:net');
const fs = require('node:fs');
const markerFd = process.env.SENTINEL_NETWORK_MARKER_FD === '3' ? 3 : 2;
delete process.env.SENTINEL_NETWORK_MARKER_FD;
const hosts = new Set();
let marked = false;

function local(host) {
 return !host || host === 'localhost' || host === '::1' || /^127\./.test(host);
}
function blocked(host) {
 if (typeof host === 'string' && host.length <= 253 && !hosts.has(host) && hosts.size < 10) {
  hosts.add(host);
  fs.writeSync(markerFd, 'SENTINEL_NETWORK_BLOCKED ' + JSON.stringify(host) + '\n');
 } else if (!marked) {
  fs.writeSync(markerFd, 'SENTINEL_NETWORK_BLOCKED\n');
 }
 marked = true;
 return Object.assign(new Error('Network disabled by Snippet Sentinel'), {code: 'ENETUNREACH'});
}
const lookup = dns.lookup;
dns.lookup = function(host, ...args) {
 if (local(host)) return lookup.call(this, host, ...args);
 const callback = args[args.length - 1];
 const error = blocked(host);
 process.nextTick(() => callback(error));
};
const promiseLookup = dns.promises.lookup;
dns.promises.lookup = async function(host, ...args) {
 if (local(host)) return promiseLookup.call(this, host, ...args);
 throw blocked(host);
};
const connect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function(...args) {
 const options = Array.isArray(args[0]) ? args[0][0] : args[0];
 const host = typeof options === 'object' ? options.host : typeof args[1] === 'string' ? args[1] : undefined;
 if (!local(host)) {
  const error = blocked(host);
  process.nextTick(() => this.destroy(error));
  return this;
 }
 return connect.apply(this, args);
};
