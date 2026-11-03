"""Synthetic long-lived descendants for the process ownership seam."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

if sys.argv[1] == "owner":
    environment = {**os.environ, "EVERNOOK_WORKER_PARENT_PID": str(os.getpid())}
    worker = subprocess.Popen([sys.executable, str(Path(__file__)), "worker"], stdout=subprocess.PIPE,
        env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
    assert worker.stdout is not None
    identities = json.loads(worker.stdout.readline())
    print(json.dumps(identities), flush=True)
else:
    from evernook.worker import protect_parent_lifetime
    protect_parent_lifetime()
    child = subprocess.Popen([sys.executable, "-u", "-c", "import os,time; print(os.getpid(),flush=True); time.sleep(20)"],
        stdout=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    assert child.stdout is not None
    print(json.dumps([os.getpid(), child.pid, int(child.stdout.readline())]), flush=True)
time.sleep(20)
