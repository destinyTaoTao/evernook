from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from probes.admission.experiment_job import ExperimentJob
from probes.admission.monitor import main, sample_job


class ExperimentJobTests(unittest.TestCase):
    def test_monitor_budget_stops_only_kernel_job_without_pid_tree_command(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-job-monitor-') as temporary:
            output = Path(temporary) / 'monitor'
            with patch.object(sys, 'argv', ['monitor', '--output', str(output), '--max-seconds', '1', '--',
                    sys.executable, '-c', 'import time;time.sleep(20)']), patch(
                    'subprocess.run', side_effect=AssertionError('no PID-tree taskkill command')):
                self.assertEqual(main(), 2)
            report = json.loads((output / 'report.json').read_bytes())
            self.assertTrue(report['root_exited'])
            self.assertEqual(report['ownership'], 'windows_job_v1')
            self.assertEqual(report['stop_reason'], 'execution_time_budget')
            self.assertIsNone(report['stop_error'])

    def test_nested_descendant_membership_and_stop_excludes_unrelated_process(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-job-scope-') as temporary:
            root = Path(temporary)
            ready = root / 'ready'
            child_code = ('from evernook.worker import protect_parent_lifetime;'
                'protect_parent_lifetime();import pathlib,os,sys,time;'
                'pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)')
            target = 'import subprocess,sys;subprocess.run([sys.executable,"-B","-c",sys.argv[2],sys.argv[1]])'
            unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'],
                creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                with (root / 'stdout').open('wb') as stdout, (root / 'stderr').open('wb') as stderr, ExperimentJob() as job:
                    child = job.start([sys.executable, '-B', '-c', target, str(ready), child_code], stdout, stderr)
                    deadline = time.monotonic() + 10
                    while not ready.exists():
                        if child.poll() is not None:
                            self.fail((root / 'stderr').read_text(encoding='utf8', errors='replace'))
                        if time.monotonic() > deadline:
                            self.fail('nested worker not ready')
                        time.sleep(.02)
                    pid = int(ready.read_text())
                    handle = job.kernel.OpenProcess(0x100410, False, pid)
                    foreign = job.kernel.OpenProcess(0x100410, False, unrelated.pid)
                    self.assertTrue(handle and foreign)
                    try:
                        self.assertTrue(job.contains(handle))
                        self.assertFalse(job.contains(foreign))
                        self.assertIn(pid, job.pids())
                        job.terminate(child)
                        self.assertEqual(job.kernel.WaitForSingleObject(handle, 3000), 0)
                        self.assertIsNone(unrelated.poll())
                    finally:
                        job.kernel.CloseHandle(handle)
                        job.kernel.CloseHandle(foreign)
            finally:
                if unrelated.poll() is None:
                    unrelated.terminate()
                unrelated.wait(timeout=5)

    def test_pid_list_reuse_cannot_attribute_foreign_memory_to_owned_job(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-job-reuse-') as temporary:
            root = Path(temporary)
            foreign = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'],
                creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                with (root / 'out').open('wb') as stdout, (root / 'err').open('wb') as stderr, ExperimentJob() as job:
                    child = job.start([sys.executable, '-c', 'import time;time.sleep(30)'], stdout, stderr)
                    with patch.object(job, 'pids', return_value=[foreign.pid]):
                        rows, failures = sample_job(job, child)
                    self.assertEqual([row['pid'] for row in rows], [child.pid])
                    self.assertEqual(failures, 1)
                    job.terminate(child)
                    self.assertIsNone(foreign.poll())
            finally:
                if foreign.poll() is None:
                    foreign.terminate()
                foreign.wait(timeout=5)
