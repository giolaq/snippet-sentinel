import ipaddress
import json
import queue
import re
import socket
import threading
import time

from .network_guard import MARKER


def blocked_hosts(output):
    hosts = []
    for line in output.splitlines():
        if not line.startswith(MARKER + ' '):
            continue
        try:
            host = json.loads(line[len(MARKER) + 1:])
            if not isinstance(host, str) or len(host) > 253:
                continue
            host = host.rstrip('.').encode('idna').decode('ascii').lower()
        except (ValueError, UnicodeError):
            continue
        try:
            ipaddress.ip_address(host)
            continue
        except ValueError:
            pass
        if not host or len(host) > 253 or not all(
                re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                for label in host.split('.')):
            continue
        if host not in hosts:
            hosts.append(host)
            if len(hosts) == 10:
                break
    return hosts


def dead_host(output, timeout=2):
    hosts = blocked_hosts(output)
    results = queue.Queue()

    def resolve(host):
        error = None
        try:
            socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            error = exc.errno
        except OSError:
            pass
        results.put((host, error))

    deadline = time.monotonic() + timeout
    started = 0
    for host in hosts:
        try:
            threading.Thread(target=resolve, args=(host,), daemon=True).start()
            started += 1
        except RuntimeError:
            break
    definitive = {socket.EAI_NONAME, getattr(socket, 'EAI_NODATA', socket.EAI_NONAME)}
    for _ in range(started):
        try:
            host, error = results.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            break
        if error in definitive:
            return host
    return None
