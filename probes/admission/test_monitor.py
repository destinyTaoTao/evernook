"""Real Windows process-tree checks for the experiment operator controls."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


class MonitorControlsTest(unittest.TestCase):
    def run_monitor(self, root: Path, script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(Path(__file__).with_name("monitor.py")),
            "--output", str(root / "monitor"), "--max-seconds", "2", "--",
            sys.executable, "-c", script, *arguments], capture_output=True, text=True,
            encoding="utf-8", timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)

    def test_deadline_stops_descendants_and_records_partial_outcome(self) -> None:
        with tempfile.TemporaryDirectory(prefix="evernook-monitor-budget-") as directory:
            root = Path(directory).resolve()
            self.assertEqual(root.parent, Path(tempfile.gettempdir()).resolve())
            ready, survived = root / "ready", root / "orphan-survived"
            script = ("import subprocess,sys,time; from pathlib import Path; "
                "subprocess.Popen([sys.executable,'-c',"
                "\"import sys,time; from pathlib import Path; time.sleep(4); Path(sys.argv[1]).touch()\","
                "sys.argv[2]],creationflags=subprocess.CREATE_NO_WINDOW); "
                "Path(sys.argv[1]).touch(); time.sleep(10)")
            result = self.run_monitor(root, script, str(ready), str(survived))
            report_path = root / "monitor/report.json"
            self.assertTrue(report_path.exists(), result.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(report["stop_reason"], "execution_time_budget")
            self.assertTrue(ready.exists(), "The real nested child must start before timeout")
            time.sleep(2.5)
            self.assertFalse(survived.exists(), "A child escaped the owned process-tree stop")

    def test_stop_file_records_operator_stop(self) -> None:
        with tempfile.TemporaryDirectory(prefix="evernook-monitor-stop-") as directory:
            root = Path(directory).resolve()
            self.assertEqual(root.parent, Path(tempfile.gettempdir()).resolve())
            result = self.run_monitor(root,
                "import sys,time; from pathlib import Path; Path(sys.argv[1]).touch(); time.sleep(10)",
                str(root / "monitor/STOP"))
            report_path = root / "monitor/report.json"
            self.assertTrue(report_path.exists(), result.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(report["stop_reason"], "operator_stop")

    def test_termination_failure_still_preserves_nonpassing_receipt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="evernook-monitor-stop-error-") as directory:
            root = Path(directory).resolve()
            self.assertEqual(root.parent, Path(tempfile.gettempdir()).resolve())
            bootstrap = ("import sys,runpy,subprocess; from pathlib import Path; "
                "sys.argv=sys.argv[1:]; sys.path.insert(0,str(Path(sys.argv[0]).parent)); "
                "exec('def fail(*args, **kwargs):\\n raise subprocess.CalledProcessError(1,args[0])'); "
                "subprocess.run=fail; runpy.run_path(sys.argv[0],run_name='__main__')")
            child = "import sys,time; from pathlib import Path; Path(sys.argv[1]).touch(); time.sleep(4)"
            result = subprocess.run([sys.executable, "-c", bootstrap,
                str(Path(__file__).with_name("monitor.py")), "--output", str(root / "monitor"),
                "--max-seconds", "2", "--", sys.executable, "-c", child, str(root / "monitor/STOP")],
                capture_output=True, text=True, encoding="utf-8", timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW)
            report = json.loads((root / "monitor/report.json").read_text(encoding="utf-8"))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(report["stop_reason"], "operator_stop")
            self.assertEqual(report["stop_error"], "CalledProcessError")
            self.assertFalse(report["root_exited"])
            time.sleep(4.2)  # The deliberately unkillable synthetic child exits on its own.


if __name__ == "__main__":
    unittest.main()
