"""Export one separately identified last-hidden-position Qwen experiment; never admit it.

Requires external tooling environment: onnx==1.20.1. Runtime dependencies are unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil

import onnx
from onnx import TensorProto, helper

from evernook.paths import external

SOURCE_SHA256 = "d43d836fc5e240df9013733ccd214972c5d21bd9ec47e574e4f1e359cf90aed0"
ASSETS = ("config.json", "generation_config.json", "tokenizer.json", "tokenizer_config.json",
          "special_tokens_map.json", "added_tokens.json", "merges.txt", "vocab.json")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def standard_check(model: onnx.ModelProto) -> str:
    try:
        onnx.checker.check_model(model)
        return "passed"
    except onnx.checker.ValidationError as error:
        return str(error)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    profile = json.loads(external(args.profile).read_text(encoding="utf-8"))
    if profile["profile"] != "qwen3-0.6b-experimental-extractive-v1" or onnx.__version__ != "1.20.1":
        raise ValueError("export_identity_mismatch")
    source = external(Path(profile["model_dir"]))
    root = external(args.output)
    if root.is_relative_to(source) or source.is_relative_to(root):
        raise ValueError("export_must_not_overlap_source")
    original = source / "onnx/model_q4.onnx"
    if digest(original) != SOURCE_SHA256:
        raise ValueError("source_model_integrity_mismatch")
    root.mkdir()  # Never overwrite a prior candidate or the original model.
    destination = root / "model"
    (destination / "onnx").mkdir(parents=True)
    model = onnx.load(original, load_external_data=False)
    original_check = standard_check(model)
    canonical_before = hashlib.sha256(model.SerializeToString()).hexdigest()
    heads = [i for i, node in enumerate(model.graph.node)
             if node.name == "/lm_head/MatMul" and node.op_type == "MatMul" and list(node.output) == ["logits"]]
    if len(heads) != 1:
        raise ValueError("unexpected_lm_head")
    index = heads[0]
    hidden = model.graph.node[index].input[0]
    outputs = [value for value in model.graph.output if value.name == "logits"]
    if len(outputs) != 1 or len(outputs[0].type.tensor_type.shape.dim) != 3:
        raise ValueError("unexpected_logits_shape")
    old_output = outputs[0].SerializeToString()
    initializers = len(model.graph.initializer)
    names = ["evernook_last_head_" + name for name in ("starts", "ends", "axes", "steps")]
    existing = {value.name for value in model.graph.initializer} | {name for n in model.graph.node for name in n.output}
    selected = "evernook_last_hidden"
    if any(name in existing for name in [*names, selected]):
        raise ValueError("export_name_collision")
    for name, value in zip(names, (-1, 2**63 - 1, 1, 1)):
        model.graph.initializer.append(helper.make_tensor(name, TensorProto.INT64, [1], [value]))
    sliced = helper.make_node("Slice", [hidden, *names], [selected], name="evernook_last_hidden_slice")
    onnx.checker.check_node(sliced)
    model.graph.node.insert(index, sliced)
    model.graph.node[index + 1].input[0] = selected
    outputs[0].type.tensor_type.shape.dim[1].ClearField("dim_param")
    outputs[0].type.tensor_type.shape.dim[1].dim_value = 1
    candidate_check = standard_check(model)
    if candidate_check != original_check:
        raise ValueError("candidate_changed_standard_checker_result")
    exported = destination / "onnx/model_q4.onnx"
    onnx.save_model(model, exported)
    # Undo precisely the allowed transformation and prove all original protobuf data survived.
    del model.graph.node[index]
    model.graph.node[index].input[0] = hidden
    del model.graph.initializer[initializers:]
    outputs[0].ParseFromString(old_output)
    if hashlib.sha256(model.SerializeToString()).hexdigest() != canonical_before:
        raise ValueError("export_changed_unrelated_graph_data")
    del model
    for name in ASSETS:
        shutil.copyfile(source / name, destination / name)
    files = {name: digest(destination / name) for name in (*ASSETS, "onnx/model_q4.onnx")}
    manifest = {"schema_version": 1, "candidate": "qwen3-0.6b-last-hidden-experimental-v1",
        "parent_model_sha256": SOURCE_SHA256, "artifacts": files, "onnx_tool_version": onnx.__version__,
        "exporter_sha256": digest(Path(__file__)), "original_graph_restored_canonical_sha256": canonical_before,
        "standard_model_checker": original_check, "inserted_slice_node_checker": "passed",
        "allowed_change": "Slice final hidden state [-1:] on axis 1 immediately before LM head; logits sequence dimension 1",
        "original_weights_and_upstream_unchanged": True, "admission": "blocked_pending_independent_validation"}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    candidate = dict(profile, profile=manifest["candidate"], model_dir=str(destination))
    (root / "profile.json").write_text(json.dumps(candidate, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"candidate": manifest["candidate"], "graph_sha256": files["onnx/model_q4.onnx"],
                      "original_data_unchanged": True}))


if __name__ == "__main__":
    main()
