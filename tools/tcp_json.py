"""Small length-prefixed TCP transport shared by management tools."""
import socket
import struct


MAX_JSON_BYTES = 1200
LENGTH = struct.Struct('!I')


def receive_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError('TCP connection closed before the frame completed')
        data.extend(chunk)
    return bytes(data)


def receive_frame(sock):
    size = LENGTH.unpack(receive_exact(sock, LENGTH.size))[0]
    if not 1 <= size <= MAX_JSON_BYTES:
        raise ValueError('invalid TCP JSON frame length')
    return receive_exact(sock, size)


def send_frame(sock, body):
    if not 1 <= len(body) <= MAX_JSON_BYTES:
        raise ValueError('invalid TCP JSON frame length')
    sock.sendall(LENGTH.pack(len(body)) + body)
