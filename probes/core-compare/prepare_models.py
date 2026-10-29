"""Download pinned public R0 candidates; never called by an offline run."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time
import urllib.request

MODELS = {
    "embedding": {
        "repo": "Xenova/paraphrase-multilingual-MiniLM-L12-v2",
        "revision": "2c4055b12046f11709e9df2c122e59ffbdc2f900",
        "weights": "onnx/model_quantized.onnx",
        "dtype": "q8",
        "base": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    },
    "extraction": {
        "repo": "onnx-community/Qwen3-0.6B-ONNX",
        "revision": "58ca03a5e7b354b480b1a82b34bde8dc7b854cc0",
        "weights": "onnx/model_q4.onnx",
        "dtype": "q4",
        "base": "Qwen/Qwen3-0.6B",
    },
}


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=120) as response:
        return json.load(response)


def download(url: str, destination: Path, expected_sha256: str | None) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    if not destination.exists():
        temporary = destination.with_suffix(destination.suffix + ".part")
        with urllib.request.urlopen(url, timeout=120) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        temporary.replace(destination)
    with destination.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if expected_sha256 and digest != expected_sha256:
        raise ValueError(f"Model integrity mismatch: {destination.name}")
    print(f"Prepared {destination.name}: {destination.stat().st_size} bytes", flush=True)
    return {"url": url, "bytes": destination.stat().st_size, "sha256": digest,
            "download_or_verify_seconds": time.monotonic() - started}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    checkout = Path(__file__).resolve().parents[2]
    if not args.output.is_absolute() or root.is_relative_to(checkout):
        raise ValueError("Model directory must be absolute and outside the checkout")
    root.mkdir(parents=True, exist_ok=True)
    manifest = {"phase": "online preparation only", "models": {}}
    for name, config in MODELS.items():
        metadata = fetch_json(f'https://huggingface.co/api/models/{config["repo"]}/revision/{config["revision"]}?blobs=true')
        files = [item for item in metadata["siblings"]
                 if ("/" not in item["rfilename"] and item["rfilename"].endswith((".json", ".txt", ".model", ".md")))
                 or item["rfilename"].startswith(config["weights"])]
        entries = {}
        for item in files:
            filename = item["rfilename"]
            url = f'https://huggingface.co/{config["repo"]}/resolve/{config["revision"]}/{filename}'
            entries[filename] = download(url, root / name / filename, item.get("lfs", {}).get("sha256"))
        base_metadata = fetch_json(f'https://huggingface.co/api/models/{config["base"]}')
        manifest["models"][name] = {**config, "base_revision": base_metadata["sha"],
                                    "base_declared_license": base_metadata.get("cardData", {}).get("license"),
                                    "files": entries}
        (root / "models-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
