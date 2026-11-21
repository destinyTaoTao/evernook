import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from probes.admission.candidate_index_scale import CANDIDATE_INDEX_NAMES, main


@unittest.skipUnless(sys.platform == "win32", "scale runtime requires local NTFS")
class CandidateIndexScaleTests(unittest.TestCase):
    def test_both_arms_reject_preexisting_named_objects_before_timing(self) -> None:
        first, second = CANDIDATE_INDEX_NAMES
        definitions = (
            [f"CREATE INDEX {first} ON messages(id)", f"CREATE INDEX {second} ON messages(id)"],
            [f"CREATE INDEX {second} ON messages(id)"],
            [f"CREATE TABLE {first.upper()} (value)"],
            [f"CREATE INDEX {first} ON messages(id)",
             f"CREATE TRIGGER {first} AFTER INSERT ON messages BEGIN SELECT 1; END"],
        )
        for objects in definitions:
            for enable in (False, True):
                with self.subTest(objects=objects, enable=enable), TemporaryDirectory() as temporary:
                    runtime = Path(temporary).resolve()
                    (runtime / "runtime.json").write_text(json.dumps({"data_dir": str(runtime)}), encoding="utf-8")
                    db = sqlite3.connect(runtime / "memory.sqlite3")
                    try:
                        db.execute("CREATE TABLE messages(id)")
                        for ddl in objects:
                            db.execute(ddl)
                        db.commit()
                    finally:
                        db.close()
                    output = runtime / "output"
                    args = ["probe", "--runtime", str(runtime), "--size", "100000", "--output", str(output),
                            "--embedding-config", str(runtime / "unused.json")]
                    if enable:
                        args.append("--enable-index")
                    with patch.object(sys, "argv", args), self.assertRaisesRegex(ValueError, "must both be absent"):
                        main()
                    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
                    self.assertEqual(len(manifest["before_objects"]), len(objects))
                    self.assertFalse((output / "timings.jsonl").exists())
