import errno
import ipaddress
import os
import socket


MARKER = 'SENTINEL_NETWORK_BLOCKED'


def local(host):
    if host in (None, '', 'localhost', b'localhost'):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def blocked():
    os.write(2, (MARKER + '\n').encode())


def install():
    lookup = socket.getaddrinfo
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def guarded_lookup(host, *args, **kwargs):
        if not local(host):
            blocked()
            raise socket.gaierror(socket.EAI_AGAIN, 'Network disabled by Snippet Sentinel')
        return lookup(host, *args, **kwargs)

    def external(sock, address):
        return sock.family in (socket.AF_INET, socket.AF_INET6) and not local(address[0])

    def guarded_connect(sock, address):
        if external(sock, address):
            blocked()
            raise OSError(errno.ENETUNREACH, 'Network disabled by Snippet Sentinel')
        return connect(sock, address)

    def guarded_connect_ex(sock, address):
        if external(sock, address):
            blocked()
            return errno.ENETUNREACH
        return connect_ex(sock, address)

    socket.getaddrinfo = guarded_lookup
    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
