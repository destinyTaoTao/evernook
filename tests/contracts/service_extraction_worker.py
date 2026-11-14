"""Synthetic blocking backend in an isolated service subprocess, never a production provider."""
from pathlib import Path
import sys
from unittest.mock import patch

from evernook.models import MemoryFailure
from evernook.service import serve


class BlockingSelector:
    processor_version = 'synthetic-blocking-extraction-v1'

    def select(self, sources: object, *, timeout: float, cancel: object) -> object:
        marker = Path(sys.argv[3])
        marker.write_text('started', encoding='utf-8')
        assert cancel is not None
        if not cancel.wait(timeout):
            raise MemoryFailure('extraction_timeout')
        marker.write_text('canceled', encoding='utf-8')
        raise MemoryFailure('extraction_canceled')


with patch('evernook.extraction.load_extraction', return_value=BlockingSelector()):
    serve(Path(sys.argv[1]), activate_owner=Path(sys.argv[2]), extraction_config=Path(sys.argv[3]))
