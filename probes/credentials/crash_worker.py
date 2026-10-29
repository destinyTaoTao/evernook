"""Abrupt-exit worker only receives public verification data and ciphertext, never a private key."""
from pathlib import Path
import sys

from nacl.signing import VerifyKey

from vault_probe import Envelope, Inbox

root = Path(sys.argv[1])
envelope = Envelope.parse((root / "input.encrypted").read_bytes())
inbox = Inbox(root, VerifyKey((root / "sender.public").read_bytes()))
inbox.receive(envelope)
if sys.argv[2] == "after-link":
    inbox.link(envelope.bound)
print("synthetic-durable-checkpoint", flush=True)
# No connection close, interpreter shutdown or finalizer: simulate process failure after commit.
__import__("os")._exit(86)
