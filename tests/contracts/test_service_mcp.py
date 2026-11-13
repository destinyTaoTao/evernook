"""Run the existing MCP conformance suite through the independent process."""
from __future__ import annotations

import subprocess
import sys
import time

import test_mcp


class ServiceMCPTests(test_mcp.MCPTests):
    def setUp(self) -> None:
        super().setUp()
        process = subprocess.Popen([sys.executable, "-B", "-m", "evernook.service",
            "--config", str(self.config)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def stop() -> None:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=15)
        self.addCleanup(stop)
        deadline = time.monotonic() + 20
        while not (self.config.parent / "memory-service.json").exists():
            if process.poll() is not None or time.monotonic() > deadline:
                self.fail("service startup failed")
            time.sleep(0.05)
