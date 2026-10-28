"""Local TCP proxy that holds the first server->client bytes of each connection for a while.

Works around a Forge 1.7.10 race against fast (localhost) servers: `FMLClientHandler.connectToServer` opens
`GuiConnecting` before it creates `playClientBlock`, so a quick LoginSuccess counts down the previous latch and the FML
handshake times out ("Timeout waiting for client thread to catch up!"). Delaying the server's first reply lets the
client thread get there first.
"""

from __future__ import annotations

import socket
import threading
import time


class DelayProxy:
    def __init__(self, target_host: str, target_port: int, delay: float):
        self.target = (target_host, target_port)
        self.delay = delay
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self._closed = False

    def start(self) -> "DelayProxy":
        threading.Thread(target=self._accept, name="delay-proxy", daemon=True).start()
        return self

    def close(self) -> None:
        self._closed = True
        self.server.close()

    def _accept(self) -> None:
        while not self._closed:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            try:
                upstream = socket.create_connection(self.target, timeout=10)
                upstream.settimeout(None)
            except OSError:
                client.close()
                continue
            threading.Thread(target=self._pipe, args=(client, upstream, 0.0), daemon=True).start()
            threading.Thread(target=self._pipe, args=(upstream, client, self.delay), daemon=True).start()

    @staticmethod
    def _pipe(source: socket.socket, sink: socket.socket, first_delay: float) -> None:
        delayed = first_delay <= 0
        try:
            while data := source.recv(65536):
                if not delayed:
                    time.sleep(first_delay)
                    delayed = True
                sink.sendall(data)
        except OSError:
            pass
        finally:
            for s in (source, sink):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.close()
