from collections.abc import Sequence
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from evernook import Change, Event, MemoryRuntime, SemanticRecall, Source, initialize
from evernook.lexical import query_expression


class KeywordScoreBoundTests(unittest.TestCase):
    def test_late_rare_hits_boundary_ties_and_ineligible_sources_match_full_ranking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config, access = initialize(Path(temporary) / "runtime", client_id="synthetic", entry="synthetic")
            runtime = MemoryRuntime(config)
            texts = ["合成" + ("".join(chr(0x4e10 + k) for k in range(i)) if i % 2 else "") for i in range(40)]
            texts.extend(["合成白鹭"] * 6)
            receipts = runtime.submit_many(access, tuple(Event(Source("synthetic", "bound", "main", str(i)),
                "user", i, None, "complete", text) for i, text in enumerate(texts)))
            runtime.refresh_lexical(access, limit=100)
            rare = [receipt.message_id for receipt in receipts[-6:]]
            current, stale, hidden = rare[:3]
            runtime.change(access, Change("hide-keyword", hidden, 1, "suppress", receipts[0].message_id, "synthetic"))
            with runtime._connection() as db:
                db.execute("UPDATE lexical_docs SET content_version=0 WHERE message_id=?", (stale,))
                db.execute("BEGIN")
                for words in (("合成",), ("合成", "白鹭"), ("白鹭",), ("鲸鲨",)):
                    expected = self.candidates(db, words, current, prefix=0)
                    sql: list[str] = []
                    db.set_trace_callback(sql.append)
                    actual = self.candidates(db, words, current, prefix=8)
                    db.set_trace_callback(None)
                    self.assertEqual(actual, expected, words)
                    self.assertTrue(set(actual).isdisjoint((current, stale, hidden)))
                    if len(words) == 2:
                        self.assertTrue(any("AND bm25(lexical)<=" in line for line in sql))
                        self.assertEqual(actual[:3], sorted(rare[3:]))

    @staticmethod
    def candidates(db: sqlite3.Connection, words: Sequence[str], current: str, *, prefix: int) -> list[str]:
        expression = " OR ".join("(" + query_expression((word,)) + ")" for word in words)
        with patch.multiple("evernook.semantic", _KEYWORD_PREFIX_SIZE=prefix, _KEYWORD_LIMIT=4, _KEYWORD_SCORE_SAMPLE=16):
            return SemanticRecall._keyword_ids(db, expression, since=None, until=None,
                                              check=lambda: 10.0, current_input_id=current)
