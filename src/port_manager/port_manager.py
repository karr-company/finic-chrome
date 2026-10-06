import os
import threading
from typing import Optional

# chromedriver's default port; one port per WebDriver session.
STARTING_PORT = 9515


class PortManager:
    def __init__(self, max_connections: Optional[int] = None):
        if max_connections is None:
            max_connections = int(os.getenv("MAX_SESSIONS", "20"))
        self.max_connections = max_connections
        self.ports = {
            i: False for i in range(STARTING_PORT, STARTING_PORT + max_connections)
        }
        # FastAPI runs handlers on the event loop, but session start/stop is
        # dispatched to a thread pool and multiple workers may share a
        # process, so allocation must be safe across threads.
        self._lock = threading.Lock()

    def acquire_port(self) -> Optional[int]:
        """Atomically reserve an available port, or return ``None``.

        Combining "find" and "mark used" under one lock guarantees two
        concurrent callers can never receive the same port.
        """
        with self._lock:
            for port, is_used in self.ports.items():
                if not is_used:
                    self.ports[port] = True
                    return port
        return None

    def get_available_port(self) -> Optional[int]:
        with self._lock:
            for port, is_used in self.ports.items():
                if not is_used:
                    return port
        return None

    def mark_port_as_used(self, port: int):
        with self._lock:
            self.ports[port] = True

    def release(self, port: int):
        with self._lock:
            if port in self.ports:
                self.ports[port] = False
