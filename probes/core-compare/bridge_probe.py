"""Measure Python/TypeScript IPC on the actual small comparison index, not a production protocol."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import time


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    root = args.run.resolve(strict=True)
    if root.is_relative_to(Path(__file__).resolve().parents[2]):
        raise ValueError("Evidence must be outside checkout")
    case = json.loads((root / "bridge-case.json").read_text(encoding="utf-8"))
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node executable not found")
    started = time.perf_counter_ns()
    with (root / "bridge-stderr.txt").open("w", encoding="utf-8") as errors:
        process = subprocess.Popen([node, str(Path(__file__).parent / "src" / "bridge.ts"), str(root / "minimal")],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True, encoding="utf-8")
        assert process.stdin is not None and process.stdout is not None
        try:
            if json.loads(process.stdout.readline()) != {"ready": True}:
                raise RuntimeError("Invalid bridge ready response")
            startup_ms = (time.perf_counter_ns() - started) / 1e6
            samples = []
            for request_id in range(60):
                start = time.perf_counter_ns()
                process.stdin.write(json.dumps({"id": request_id, **case}, ensure_ascii=False) + "\n")
                process.stdin.flush()
                response = json.loads(process.stdout.readline())
                roundtrip_ms = (time.perf_counter_ns() - start) / 1e6
                if response["id"] != request_id or not response["hits"]:
                    raise RuntimeError("Invalid bridge response")
                samples.append({"id": request_id, "roundtripMs": roundtrip_ms, "nodePathMs": response["pathMs"],
                                "overheadMs": roundtrip_ms - response["pathMs"], "sourceIds": [hit["id"] for hit in response["hits"]]})
            result = {"python": __import__("sys").version, "node": node, "startupMs": startup_ms,
                      "boundary": "persistent stdio; precomputed real query vector; no embedding inference in measured roundtrip", "samples": samples}
            (root / "bridge-results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdout.close()
        if process.returncode:
            raise RuntimeError(f"Bridge exited with code {process.returncode}")
    print(f"Recorded {len(samples)} real stdio round trips in {root}")


if __name__ == "__main__":
    main()
