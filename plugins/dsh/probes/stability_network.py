"""Installed as sitecustomize only in the disposable stability environment."""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import socket
import time
from typing import Any


def local(host: Any) -> bool:
    if host in (None, 'localhost', b'localhost'):
        return True
    try:
        return ipaddress.ip_address(host.decode() if isinstance(host, bytes) else host).is_loopback
    except (ValueError, TypeError):
        return False


log = os.environ.get('EVERNOOK_STABILITY_NETWORK_LOG')
selection = Path(__file__).with_name('stability-network-journal.txt')
if not log and selection.is_file():
    log = selection.read_text(encoding='utf-8').strip()
if log:
    def deny(kind: str) -> None:
        assert log is not None
        with open(log, 'a', encoding='utf-8') as output:
            output.write(json.dumps({'runtime': 'python', 'kind': kind, 'pid': os.getpid(), 'at': time.time()}) + '\n')
        raise OSError('synthetic_external_network_denied')

    getaddrinfo = socket.getaddrinfo

    def guarded_lookup(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not local(host):
            deny('dns')
        return getaddrinfo(host, *args, **kwargs)

    socket.getaddrinfo = guarded_lookup
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        if self.family != getattr(socket, 'AF_UNIX', None) and (not isinstance(address, tuple) or not local(address[0])):
            deny('tcp')
        return connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> Any:
        if self.family != getattr(socket, 'AF_UNIX', None) and (not isinstance(address, tuple) or not local(address[0])):
            deny('tcp')
        return connect_ex(self, address)

    setattr(socket.socket, 'connect', guarded_connect)
    setattr(socket.socket, 'connect_ex', guarded_connect_ex)
