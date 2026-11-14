from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evernook.embedding import LocalMiniLM, ServiceEmbedding, load_embedding
from evernook.models import MemoryFailure


class EmbeddingProviderTests(unittest.TestCase):
    def test_service_binding_pins_valid_config_and_requires_restart_after_repair(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-binding-') as temporary:
            root = Path(temporary)
            path = root / 'embedding.json'
            config = {'schema_version': 2, 'provider': 'local-minilm', 'node': str(root / 'node'),
                      'transformers_dir': str(root / 'deps'), 'model_dir': str(root / 'model-a')}
            path.write_text(json.dumps(config), encoding='utf-8')
            binding = ServiceEmbedding(path)
            first = binding.current()
            path.write_text(json.dumps(config, indent=2), encoding='utf-8')
            self.assertIs(binding.current(), first)
            config['model_dir'] = str(root / 'model-b')
            path.write_text(json.dumps(config), encoding='utf-8')
            with self.assertRaisesRegex(MemoryFailure, 'embedding_config_changed'):
                binding.current()
            self.assertEqual(first.space, ServiceEmbedding(path).current().space)
            path.write_text('{', encoding='utf-8')
            initially_invalid = ServiceEmbedding(path)
            with self.assertRaisesRegex(MemoryFailure, 'invalid_config'):
                initially_invalid.current()
            path.write_text(json.dumps(config), encoding='utf-8')
            with self.assertRaisesRegex(MemoryFailure, 'embedding_config_changed'):
                initially_invalid.current()
            self.assertIsInstance(ServiceEmbedding(path).current(), LocalMiniLM)
            path.write_text('{}', encoding='utf-8')
            invalid_schema = ServiceEmbedding(path)
            with self.assertRaisesRegex(MemoryFailure, 'invalid_embedding_config'):
                invalid_schema.current()
            path.write_text(json.dumps(config), encoding='utf-8')
            with self.assertRaisesRegex(MemoryFailure, 'embedding_config_changed'):
                invalid_schema.current()

    def test_concurrent_config_change_cannot_swap_inflight_backend(self) -> None:
        from evernook.embedding import _load
        with tempfile.TemporaryDirectory(prefix='evernook-binding-race-') as temporary:
            root = Path(temporary)
            path = root / 'embedding.json'
            config = {'schema_version': 2, 'provider': 'local-minilm', 'node': str(root / 'node'),
                      'transformers_dir': str(root / 'deps'), 'model_dir': str(root / 'model-a')}
            path.write_text(json.dumps(config), encoding='utf-8')
            binding = ServiceEmbedding(path)
            first = binding.current()
            def change_during_validation(config_path: Path):  # type: ignore[no-untyped-def]
                result = _load(config_path)
                config['model_dir'] = str(root / 'model-b')
                path.write_text(json.dumps(config), encoding='utf-8')
                return result
            with patch('evernook.embedding._load', side_effect=change_during_validation):
                self.assertIs(binding.current(), first)
            with self.assertRaisesRegex(MemoryFailure, 'embedding_config_changed'):
                binding.current()

    def test_legacy_and_explicit_config_share_pinned_space_and_reject_unknown_providers(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-embedding-provider-') as temporary:
            root = Path(temporary)
            config = root / 'embedding.json'
            legacy = {'schema_version': 1, 'node': str(root / 'node'),
                      'transformers_dir': str(root / 'transformers'), 'model_dir': str(root / 'model')}
            config.write_text(json.dumps(legacy), encoding='utf-8')
            old = load_embedding(config)
            self.assertIsInstance(old, LocalMiniLM)
            explicit = {**legacy, 'schema_version': 2, 'provider': 'local-minilm'}
            config.write_text(json.dumps(explicit), encoding='utf-8')
            selected = load_embedding(config)
            self.assertIsInstance(selected, LocalMiniLM)
            self.assertEqual(selected.space, old.space)
            for invalid in ({**explicit, 'provider': 'cloud'}, {**explicit, 'provider': 'os.system'},
                            {**explicit, 'unexpected': 'x'}, {**legacy, 'schema_version': True}):
                config.write_text(json.dumps(invalid), encoding='utf-8')
                with self.assertRaisesRegex(MemoryFailure, 'invalid_embedding_config'):
                    load_embedding(config)


if __name__ == '__main__':
    unittest.main()
