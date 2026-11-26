import importlib
import unittest


class PlatformImportTests(unittest.TestCase):
    def test_old_imports_share_platform_state_and_patch_targets(self) -> None:
        for name in ("paths", "windows_acl", "worker", "service_gate", "secure_ntfs"):
            with self.subTest(module=name):
                original = importlib.import_module("evernook." + name)
                implementation = importlib.import_module("evernook.platform." + name)
                self.assertIs(original, implementation)
