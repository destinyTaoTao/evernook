"""Synthetic unresponsive child for cancellation/timeout cleanup, never writes source words."""
import sys
import time

sys.stdin.buffer.read()
while True:
    time.sleep(1)
