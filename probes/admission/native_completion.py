"""Transport-only parsing for the pinned llama.cpp b12360 diagnostic runner."""
from __future__ import annotations

import re


def binary_input_bytes(stderr: bytes, expected: int) -> int:
    counts = re.findall(rb'^Read (\d+) bytes from binary file [^\r\n]+\r?$', stderr, re.MULTILINE)
    if type(expected) is not int or not 1 <= expected <= 262144 or len(counts) != 1 or int(counts[0]) != expected:
        raise ValueError('native_input_transport_mismatch')
    return int(counts[0])


def token_usage(stderr: bytes, cap: int) -> tuple[int, int]:
    # Verbose native token-piece logs can contain incomplete UTF-8 sequences.
    # Read only the actual logger's ASCII counters, never decode the whole log.
    prompt = re.findall(rb'^\d+\.\d+\.\d+\.\d+ I llama_completion: number of tokens in prompt = (\d+)\r?$', stderr, re.MULTILINE)
    remaining = re.findall(rb'^\d+\.\d+\.\d+\.\d+ D n_remain: (\d+)\r?$', stderr, re.MULTILINE)
    if type(cap) is not int or not 1 <= cap <= 512 or len(prompt) != 1 or not 1 <= len(remaining) <= cap:
        raise ValueError('native_metering_unavailable')
    inputs = int(prompt[0])
    counts = [int(value) for value in remaining]
    if not 1 <= inputs <= 4096 or counts != list(range(cap - 1, cap - len(counts) - 1, -1)):
        raise ValueError('native_metering_unavailable')
    # The pinned completion loop decrements n_remain for every generated ID,
    # including the final EOG; all thought tokens consume the same cap.
    return inputs, len(counts)


def completion_text(stdout: bytes, *, thinking: bool) -> tuple[str, bool, int | None]:
    if len(stdout) > 65536:
        raise ValueError('native_output_size')
    try:
        text = stdout.decode('utf-8', errors='strict').strip()
    except UnicodeError:
        raise ValueError('native_output_encoding') from None
    marker = ' [end of text]'
    eog = text.endswith(marker)
    if eog:
        text = text[:-len(marker)].rstrip()
    reasoning: int | None = None
    if thinking:
        if text.count('</think>') != 1:
            raise ValueError('native_incomplete_thinking')
        thought, text = text.split('</think>', 1)
        reasoning, text = len(thought.strip()), text.strip()
        if not text:
            raise ValueError('native_incomplete_thinking')
    # No JSON extraction, source repair, rule or semantic answer selection.
    return text, eog, reasoning
