from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from evernook.embedding import LocalMiniLM, load_embedding
from evernook.models import MemoryFailure


class EmbeddingProviderTests(unittest.TestCase):
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
