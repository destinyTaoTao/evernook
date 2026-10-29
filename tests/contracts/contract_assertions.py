"""Shared original-evidence assertions at the core and MCP public boundaries."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any
import unittest

from evernook import Event


def assert_original_roundtrip(case: unittest.TestCase, event: Event, receipt: dict[str, Any], record: dict[str, Any]) -> None:
    case.assertEqual(receipt["status"], "committed")
    case.assertTrue(receipt["original_saved"])
    case.assertEqual(receipt["representation"], "original")
    case.assertTrue(receipt["message_id"])
    case.assertTrue(receipt["received_at"])
    case.assertEqual(record["event"], asdict(event))
    case.assertEqual(record["receipt"], receipt)
