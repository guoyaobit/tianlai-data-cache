"""Bounded REQ/ROUTER transport for TLSTORE1.

The ROUTER is owned exclusively by the listener thread. Workers exchange
messages with it via bounded per-session queues and never access the socket.
"""
import queue
import threading

try:
    import zmq
except ImportError as error:
    raise SystemExit('pyzmq is required by the storage receiver') from error

MAX_MESSAGE = 13 * 1024 * 1024
MAX_SESSIONS = 14
STOP = object()


class MessageStream:
    """Provide individual ZeroMQ messages to the storage parser and reply after processing completes."""
    def __init__(self, identity, outgoing):
        self.identity = identity
        self.incoming = queue.Queue(maxsize=1)
        self.outgoing = outgoing
        self.timeout = 120
        self.message = None
        self.cursor = 0
        self.pending_reply = False
        self.in_flight = True  # owned by the ROUTER thread

    def settimeout(self, value):
        self.timeout = value

    def start_message(self):
        """Acknowledge the previously processed message, then wait for the next message in the current session."""
        if self.message is not None:
            self.finish_message()
            self.outgoing.put((self, b'OK'))
            self.pending_reply = False
            self.message = None
        try:
            message = self.incoming.get(timeout=self.timeout)
        except queue.Empty as error:
            raise TimeoutError('ZeroMQ message timed out') from error
        if message is STOP:
            raise OSError('receiver stopping')
        self.message = message
        self.cursor = 0
        self.pending_reply = True

    def finish_message(self):
        if self.message is None or self.cursor != len(self.message):
            raise ValueError('invalid ZeroMQ message length')

    def recv_into(self, view):
        if self.message is None or self.cursor == len(self.message):
            raise EOFError('truncated ZeroMQ message')
        size = min(len(view), len(self.message) - self.cursor)
        view[:size] = memoryview(self.message)[self.cursor:self.cursor + size]
        self.cursor += size
        return size

    def read_view(self, count):
        """Return a read-only view of the current message without copying data blocks or crossing message boundaries."""
        if self.message is None or count < 0 or count > len(self.message) - self.cursor:
            raise EOFError('truncated ZeroMQ message')
        start = self.cursor
        self.cursor += count
        return memoryview(self.message)[start:self.cursor].toreadonly()

    def sendall(self, value):
        """Send the final persistence result; the END message must be consumed completely first."""
        self.finish_message()
        if not self.pending_reply or len(value) > 1204:
            raise ValueError('invalid final result')
        self.outgoing.put((self, bytes(value)))
        self.pending_reply = False

    def fail(self, error):
        if self.pending_reply:
            self.outgoing.put((self, b'ERROR:' + str(error).encode('utf-8', errors='replace')[:990]))
            self.pending_reply = False

    def close(self):
        self.outgoing.put((self, None))


def listen_zmq(receiver, address, lane, stop=None):
    """Send and receive messages in one ROUTER thread while dedicated workers persist data for each node."""
    sessions = {}
    outgoing = queue.SimpleQueue()
    context = zmq.Context.instance()
    router = context.socket(zmq.ROUTER)
    router.setsockopt(zmq.LINGER, 0)
    router.setsockopt(zmq.ROUTER_MANDATORY, 1)
    router.setsockopt(zmq.SNDHWM, 1)
    router.setsockopt(zmq.RCVHWM, 1)
    router.setsockopt(zmq.MAXMSGSIZE, MAX_MESSAGE)
    try:
        router.bind(f'tcp://{address[0]}:{address[1]}')
    except Exception:
        router.close()
        raise
    poller = zmq.Poller()
    poller.register(router, zmq.POLLIN)
    print(f'storage ZeroMQ lane {lane} listening on {address}', flush=True)

    def reply(identity, body):
        try:
            router.send_multipart([identity, b'', body], flags=zmq.DONTWAIT)
        except zmq.ZMQError as error:
            # Disconnects, full queues and stale identities must never look like
            # a delivered durable acknowledgement.
            print(f'receiver reply failed: {error}', flush=True)

    def worker(stream, peer):
        try:
            receiver.handle(stream, (peer, 0), lane)
        except Exception as error:
            print(f'receiver worker failed: {error}', flush=True)
            stream.fail(error)
            stream.close()

    try:
        while stop is None or not stop.is_set():
            while True:
                try:
                    stream, result = outgoing.get_nowait()
                except queue.Empty:
                    break
                if sessions.get(stream.identity) is not stream:
                    continue
                if result is None:
                    del sessions[stream.identity]
                else:
                    reply(stream.identity, result)
                    stream.in_flight = False
            if not dict(poller.poll(1)).get(router, 0) & zmq.POLLIN:
                continue
            # Do not accumulate an unbounded multipart envelope in Python.
            parts = []
            part_count = 0
            while True:
                part = router.recv(copy=False)
                part_count += 1
                if part_count <= 3:
                    parts.append(part)
                if not router.getsockopt(zmq.RCVMORE):
                    break
            identity = bytes(parts[0]) if parts else b''
            if part_count != 3 or bytes(parts[1]) != b'':
                if identity:
                    reply(identity, b'ERROR:invalid message envelope')
                continue
            try:
                peer = parts[2].get('Peer-Address')
                if isinstance(peer, bytes):
                    peer = peer.decode('ascii')
            except (KeyError, zmq.ZMQError, UnicodeError):
                peer = None
            if receiver.allowed_sources and (not peer or peer not in receiver.allowed_sources):
                reply(identity, b'ERROR:source address not allowed or unavailable')
                continue
            stream = sessions.get(identity)
            if stream is None:
                if len(sessions) >= MAX_SESSIONS:
                    reply(identity, b'ERROR:receiver lanes busy')
                    continue
                if len(parts[2]) > 1204:
                    reply(identity, b'ERROR:metadata exceeds limit')
                    continue
                stream = MessageStream(identity, outgoing)
                sessions[identity] = stream
                # Retain Frame ownership until the worker finishes to avoid copying the entire message.
                stream.incoming.put_nowait(parts[2])
                threading.Thread(target=worker, args=(stream, peer), daemon=True).start()
            else:
                if stream.in_flight:
                    reply(identity, b'ERROR:unexpected concurrent message')
                    continue
                try:
                    stream.incoming.put_nowait(parts[2])
                    stream.in_flight = True
                except queue.Full:
                    reply(identity, b'ERROR:unexpected concurrent message')
    finally:
        for stream in sessions.values():
            try:
                stream.incoming.put_nowait(STOP)
            except queue.Full:
                pass
        router.close()
