import os
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

    def get_available_port(self) -> Optional[int]:
        for port, is_used in self.ports.items():
            if not is_used:
                return port
        return None

    def mark_port_as_used(self, port: int):
        self.ports[port] = True

    def release(self, port: int):
        self.ports[port] = False
