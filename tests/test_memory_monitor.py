import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import memory_monitor as monitor


class MemoryMonitorTests(unittest.TestCase):
    def test_record_has_correlation_and_system_fields(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(monitor, 'LOG', Path(folder)/'memory.jsonl'):
            monitor.record('test')
            item = json.loads(next(Path(folder).glob('memory-*.jsonl')).read_text(encoding='utf-8'))
            for key in ('run_id', 'build_id', 'uptime_seconds', 'system', 'context', 'processes'):
                self.assertIn(key, item)

    def test_record_includes_heap_and_runtime_metrics(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(monitor, 'LOG', Path(folder) / 'memory.jsonl'),
            patch.object(
                monitor,
                'python_heap_summary',
                return_value={'current_mb': 12.5, 'peak_mb': 20.0},
            ),
            patch.dict(monitor._runtime_metrics, {}, clear=True),
        ):
            monitor.update_runtime_metrics(log_view_blocks=123)
            monitor.increment_runtime_metric('scheduler_events_emitted')
            monitor.record('test')
            item = json.loads(
                next(Path(folder).glob('memory-*.jsonl')).read_text(encoding='utf-8')
            )

        self.assertEqual(item['python_heap']['current_mb'], 12.5)
        self.assertEqual(item['runtime_metrics']['log_view_blocks'], 123)
        self.assertEqual(item['runtime_metrics']['scheduler_events_emitted'], 1)

    def test_windows_ctypes_structures_are_reused(self):
        entry_type = monitor._ProcessEntry
        counters_type = monitor._ProcessCounters
        monitor.snapshot()
        monitor.snapshot()
        self.assertIs(monitor._ProcessEntry, entry_type)
        self.assertIs(monitor._ProcessCounters, counters_type)

    def test_export_excludes_business_files_and_sanitizes_legacy_result(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(monitor, 'LOG', Path(folder)/'memory.jsonl'):
            root = Path(folder)
            (root/'gui.log').write_text('SECRET COOKIE', encoding='utf-8')
            (root/'memory-old.jsonl').write_text(json.dumps({'result':'PRIVATE ORDER BODY'})+'\n{', encoding='utf-8')
            monitor.export_bundle(root/'bundle.zip')
            with zipfile.ZipFile(root/'bundle.zip') as archive:
                content = b''.join(archive.read(name) for name in archive.namelist())
                self.assertNotIn(b'SECRET COOKIE', content)
                self.assertNotIn(b'PRIVATE ORDER BODY', content)
                self.assertNotIn('gui.log', archive.namelist())

    def test_periodic_sampler_does_not_depend_on_gui(self):
        called = threading.Event()
        with patch.object(monitor, 'record', side_effect=lambda *a: called.set()):
            stop = monitor.start_periodic(interval=0.01)
            try:
                self.assertTrue(called.wait(2))
            finally:
                stop.set()

    def test_retention_only_deletes_old_diagnostic_files(self):
        import os
        with tempfile.TemporaryDirectory() as folder, patch.object(monitor, 'LOG', Path(folder)/'memory.jsonl'), patch.object(monitor, '_last_cleanup', 0):
            old = Path(folder)/'memory-old.jsonl'
            other = Path(folder)/'gui.log'
            old.touch(); other.touch()
            os.utime(old, (1,1)); os.utime(other, (1,1))
            monitor._cleanup()
            self.assertFalse(old.exists())
            self.assertTrue(other.exists())

    def test_focus_returns_false_when_process_tree_has_no_browser(self):
        with patch.object(monitor, 'snapshot', return_value=[]):
            self.assertFalse(monitor.focus_browser_window(12345))

    def test_focus_maximizes_browser_before_foregrounding(self):
        api = MagicMock()
        api.BringWindowToTop.return_value = True
        api.SetForegroundWindow.return_value = True
        with patch.object(monitor, '_browser_windows', return_value=([101], api)):
            self.assertTrue(monitor.focus_browser_window(12345))
        api.ShowWindow.assert_called_once_with(101, 3)
        api.BringWindowToTop.assert_called_once_with(101)
        api.SetForegroundWindow.assert_called_once_with(101)

    def test_topmost_targets_only_discovered_browser_windows(self):
        api = MagicMock()
        api.SetWindowPos.return_value = True
        with patch.object(monitor, '_browser_windows', return_value=([101, 202], api)):
            self.assertTrue(monitor.set_browser_topmost(12345, True))
        self.assertEqual(api.SetWindowPos.call_count, 2)
