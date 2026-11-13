from __future__ import annotations

import unittest

from probes.admission.native_completion import binary_input_bytes, completion_text, token_usage


class NativeCompletionTests(unittest.TestCase):
    def test_native_read_must_confirm_all_compiled_bytes(self) -> None:
        header = b'Read 20 bytes from binary file C:\\synthetic\\prompt.txt\r\n'
        self.assertEqual(binary_input_bytes(header + b'token piece \xe4\xba', 20), 20)
        for invalid in (b'', header, header + header):
            with self.subTest(stderr=invalid), self.assertRaisesRegex(ValueError, 'native_input_transport'):
                binary_input_bytes(invalid, 21)

    def test_binary_token_logging_does_not_invalidate_ascii_metering(self) -> None:
        stderr = (b'0.00.010.001 I token piece: \xe4\xba\n'
            b'0.00.010.002 I llama_completion: number of tokens in prompt = 618\r\n'
            b'0.00.010.003 D n_remain: 511\r\n0.00.010.004 D n_remain: 510\r\n')
        self.assertEqual(token_usage(stderr, 512), (618, 2))
        with self.assertRaisesRegex(ValueError, 'metering'):
            token_usage(stderr + b'0.00.010.005 D n_remain: 500\n', 512)
        with self.assertRaisesRegex(ValueError, 'metering'):
            token_usage(b'model prose: number of tokens in prompt = 618\nn_remain: 510', 512)

    def test_transport_marker_is_not_a_license_to_repair_generated_json(self) -> None:
        text, eog, reasoning = completion_text(b'{"action":"abstain","support":{}} [end of text]\r\n\r\n', thinking=False)
        self.assertEqual(text, '{"action":"abstain","support":{}}')
        self.assertTrue(eog)
        self.assertIsNone(reasoning)
        self.assertEqual(completion_text(b'prose {"x":1} [end of text]\n', thinking=False)[0], 'prose {"x":1}')
        self.assertEqual(completion_text(b'{"x": [end of text]\n', thinking=False)[0], '{"x":')
        with self.assertRaisesRegex(ValueError, 'encoding'):
            completion_text(b'\xe4\xba', thinking=False)

    def test_thinking_delimiter_is_strict_and_empty_reasoning_is_explicit(self) -> None:
        final, _, reasoning = completion_text(b'\n</think>\n{"action":"abstain","support":{}} [end of text]\n', thinking=True)
        self.assertEqual(reasoning, 0)
        self.assertTrue(final.startswith('{'))
        for invalid in (b'{"x":1}', b'a</think>b</think>{"x":1}', b'a</think>'):
            with self.subTest(value=invalid), self.assertRaisesRegex(ValueError, 'thinking'):
                completion_text(invalid, thinking=True)
