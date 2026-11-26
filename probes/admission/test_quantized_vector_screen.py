from pathlib import Path
import sqlite3
import struct
from tempfile import TemporaryDirectory
import unittest

from probes.admission.quantized_vector_screen import screen


class QuantizedVectorScreenTests(unittest.TestCase):
    def test_full_shortlist_recovers_exact_order_inside_one_space(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.sqlite3"
            db = sqlite3.connect(path)
            try:
                db.execute("CREATE TABLE semantic_chunks_v1(space_id TEXT,message_id TEXT,start INTEGER,vector BLOB)")
                for i in range(8):
                    vector = struct.pack("<8f", *(float(i == j) for j in range(8)))
                    db.execute("INSERT INTO semantic_chunks_v1 VALUES ('main',?,0,?)", (str(i), vector))
                db.execute("INSERT INTO semantic_chunks_v1 VALUES ('other','other',0,?)", (vector,))
                db.commit()
            finally:
                db.close()
            before = path.read_bytes()
            result = screen(path, queries=3, shortlist=8, top=4)
            self.assertEqual(result["chunks"], 8)
            self.assertEqual(result["binary_payload_bytes"], 8)
            self.assertEqual(result["int8_payload_bytes"], 64)
            cases = result["queries"]
            assert isinstance(cases, list)
            self.assertEqual(len(cases), 3)
            for case in cases:
                for encoding in ("binary", "int8"):
                    self.assertTrue(case[encoding]["identical"])
            self.assertEqual(path.read_bytes(), before)
