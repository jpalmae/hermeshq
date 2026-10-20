import unittest
from unittest.mock import AsyncMock

from hermeshq.services.hermes_version_manager import HermesVersionManager


def _manager():
    manager = HermesVersionManager.__new__(HermesVersionManager)
    manager._bundled_version_cache = None
    manager._run_capture = AsyncMock(return_value=(0, "hermes 1.2.3"))
    manager._extract_version = HermesVersionManager._extract_version.__get__(manager)
    manager._VERSION_RE = HermesVersionManager._VERSION_RE
    return manager


class BundledVersionCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_lookup_does_not_spawn_another_process(self):
        manager = _manager()
        first = await manager.detect_bundled_version()
        second = await manager.detect_bundled_version()
        self.assertEqual(first, second)
        self.assertIsNotNone(first)
        self.assertEqual(manager._run_capture.await_count, 1)

    async def test_failed_detection_is_not_cached(self):
        manager = _manager()
        manager._run_capture = AsyncMock(return_value=(1, "boom"))
        self.assertIsNone(await manager.detect_bundled_version())
        calls_after_first = manager._run_capture.await_count
        manager._run_capture = AsyncMock(return_value=(0, "hermes 2.0.0"))
        self.assertEqual(await manager.detect_bundled_version(), "2.0.0")
        self.assertGreaterEqual(calls_after_first, 1)

    async def test_resolve_runtime_without_version_reuses_cached_value(self):
        manager = _manager()
        manager.get_catalog_entry = AsyncMock()
        selection_a = await manager.resolve_runtime(None)
        selection_b = await manager.resolve_runtime(None)
        self.assertEqual(selection_a.detected_version, selection_b.detected_version)
        self.assertEqual(selection_a.source, "bundled")
        self.assertEqual(manager._run_capture.await_count, 1)


if __name__ == "__main__":
    unittest.main()
