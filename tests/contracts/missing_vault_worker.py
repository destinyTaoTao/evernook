"""Synthetic crash probe at the missing-vault marker/config boundary."""
import os
from pathlib import Path
import sys

import evernook.service as service

config, access = map(Path, sys.argv[1:3])
phase = sys.argv[3]
original = getattr(service, '_atomic_json')

def interrupted(path: Path, value: object) -> None:
    if isinstance(value, dict) and value.get('service_mode') is True:
        if phase == 'before':
            os._exit(87)
        original(path, value)
        os._exit(88)
    original(path, value)

setattr(service, '_atomic_json', interrupted)
service.serve(config, activate_owner=access, activate_restored_without_vault=True)
