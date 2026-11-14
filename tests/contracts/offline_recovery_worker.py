"""Crash-injection worker; arguments and password are synthetic test data only."""
import os
from pathlib import Path
import sys

from evernook.offline_recovery import ACKNOWLEDGEMENT, OfflineRecovery

source, destination, recovery_file, phase = sys.argv[1:]
original = os.rename
with OfflineRecovery(Path(source), Path(destination), Path(recovery_file), b'synthetic-recovery-password-456') as review:
    def interrupted(src: object, dst: object) -> None:
        if phase == 'before':
            os._exit(85)
        original(src, dst)  # type: ignore[arg-type]
        os._exit(86)
    os.rename = interrupted  # type: ignore[assignment]
    review.publish(ACKNOWLEDGEMENT)
