from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from evernook import MemoryFailure, MemoryRuntime, initialize
from evernook.runtime_mode import runtime_mode


class RuntimeModeTests(unittest.TestCase):
    def test_v1_modes_and_restore_marker_combinations(self) -> None:
        base = {"schema_version": 1, "data_dir": "synthetic", "busy_timeout_ms": 100}
        self.assertEqual(runtime_mode(base), "embedded")
        for flags, expected in (({}, "ordinary"), ({"protected_single_binding": True}, "single"),
                                ({"protected_multi_binding": True}, "multi")):
            for restore in ({}, {"restored_missing_vault": True}):
                self.assertEqual(runtime_mode({**base, "service_mode": True, **flags, **restore}), expected)
        for invalid in ({"restored_missing_vault": True}, {"protected_single_binding": True},
                        {"protected_multi_binding": True}, {"unknown": True},
                        {"service_mode": True, "protected_single_binding": True, "protected_multi_binding": True}):
            with self.assertRaisesRegex(MemoryFailure, "unsupported_config"):
                runtime_mode({**base, **invalid})

    def test_all_persisted_mode_markers_require_literal_true(self) -> None:
        with TemporaryDirectory(prefix="evernook-mode-") as directory:
            config, _ = initialize(Path(directory) / "ordinary", client_id="synthetic", entry="test")
            original = json.loads(config.read_text(encoding="utf-8"))
            for key in ("service_mode", "protected_single_binding", "protected_multi_binding", "restored_missing_vault"):
                for invalid in (False, 1, None, "true"):
                    with self.subTest(key=key, value=invalid):
                        config.write_text(json.dumps({**original, "service_mode": True, key: invalid}), encoding="utf-8")
                        with self.assertRaises(MemoryFailure) as failure:
                            MemoryRuntime(config, service_owner=True)
                        self.assertEqual(failure.exception.code, "unsupported_config")
