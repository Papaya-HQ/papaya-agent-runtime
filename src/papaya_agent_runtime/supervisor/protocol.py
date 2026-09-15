"""Newline-delimited JSON request/response framing for the supervisor socket."""

from __future__ import annotations

import json
import socket


def encode(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode("utf-8")


def send_request(sock: socket.socket, request: dict) -> dict:
    sock.sendall(encode(request))
    return _read_json_line(sock)


def _read_json_line(sock: socket.socket) -> dict:
    buf = bytearray()
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf.extend(chunk)
        if b"\n" in chunk:
            break
    line = bytes(buf).split(b"\n", 1)[0]
    if not line:
        return {}
    return json.loads(line.decode("utf-8"))


def read_request(conn_file) -> dict | None:
    line = conn_file.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return {}
    return json.loads(line)
