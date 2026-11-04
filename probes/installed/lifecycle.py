"""Verify important/control/restore invariants through an actual installed wheel."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import importlib.metadata
import json
from pathlib import Path

import evernook
from evernook import CandidateDraft, CandidateWorkflow, Change, Event, EvidenceSpan, ImportantMark, MemoryFailure, MemoryRuntime, Source, initialize, pair_client
from evernook.archive import capture_control_checkpoint, replay_archive_controls
from evernook.automatic_preferences import PreferenceUpdates
from evernook.facts import FactDecision, FactRecord, FactRegistry
from evernook.paths import external
from evernook.purge import PurgeDecision, PurgeWorkflow
from evernook.runtime_restore import publish_replayed_runtime


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = external(args.output)
    root.mkdir()
    config, owner = initialize(root / "runtime", client_id="synthetic-owner", entry="installed-control", allow_archive_review=True)
    runtime = MemoryRuntime(config)
    ordinary = pair_client(config, client_id="synthetic-reader", entry="ordinary",
        access_path=config.parent / "mcp-access-ordinary.json", scopes=("capture", "recall"))
    checks = {}
    counter = 0

    def save(text: str) -> tuple[Event, str]:
        nonlocal counter
        counter += 1
        event = Event(Source("installed-control", "synthetic", "main", str(counter)), "user", counter,
                      "2026-11-04T00:00:00Z", "complete", text)
        return event, runtime.submit(owner, event).message_id

    original = "合成重要原话：37.50元，🙂和e\u0301；\r\n保留逐字换行。"
    original_event, original_id = save(original)
    important = runtime.mark_important(owner, ImportantMark(original_id, "original", 1, "synthetic", None, None, original))
    stored_original = runtime.read(ordinary, original_id)
    checks["ordinary_original_exact"] = stored_original is not None and stored_original.event.text == original
    checks["important_exact"] = runtime.read_important(ordinary, important.important_id).text == original
    checks["idempotent_source"] = runtime.submit(owner, original_event).message_id == original_id
    workflow = CandidateWorkflow(runtime)
    facts = FactRegistry(runtime)

    def fact(request: str, text: str, revision: int, previous: FactRecord | None = None) -> FactRecord:
        _, message_id = save(text)
        lease = workflow.begin(owner, (message_id,), processor_version="synthetic-installed-explicit-v1")
        if lease.token is None:
            raise ValueError("fixture_candidate_not_claimed")
        workflow.complete(owner, lease.status.run_id, lease.token, (CandidateDraft(text, "user_statement",
            (EvidenceSpan(message_id, 1, "original", 1, 0, len(text), text),)),))
        candidate = workflow.candidates(owner, lease.status.run_id)[0]
        return facts.decide(owner, FactDecision(request, "synthetic-person", "residence", candidate.candidate_id,
            message_id, revision, "correction" if previous else "assert", "2020-01-01T00:00:00Z",
            replaces_id=previous.fact_id if previous else None))

    old = fact("old-home", "合成：小林住杭州。", 0)
    corrected = fact("correct-home", "合成：先前说错，小林住上海。", 1, old)
    checks["correction_current"] = facts.query(ordinary, old.subject_id, old.predicate, valid_at="2021-01-01T00:00:00Z").fact == corrected
    checks["prior_knowledge_preserved"] = facts.query(ordinary, old.subject_id, old.predicate,
        valid_at="2021-01-01T00:00:00Z", known_at=old.recorded_at).fact == old
    _, hidden_id = save("合成不再召回的青色风铃。")
    _, evidence_id = save("合成用户明确要求不再召回刚才那条风铃记录。")
    runtime.change(owner, Change("installed-suppress", hidden_id, 1, "suppress", evidence_id, "synthetic"))
    checks["suppressed_unavailable"] = runtime.read(ordinary, hidden_id) is None
    checks["suppressed_search_empty"] = runtime.search(ordinary, "青色风铃", mode="exact").hits == ()
    erased_event, erased_id = save("合成需要彻底删除的橙色纸船。")
    purge = PurgeWorkflow(runtime)
    preview = purge.preview(owner, "installed-purge", (erased_id,))
    _, confirmation_id = save("合成用户明确确认彻底删除刚才预览的唯一纸船来源。")
    decision = PurgeDecision(preview.request_id, confirmation_id)
    purged = purge.purge(owner, decision)
    checks["purge_idempotent"] = purge.purge(owner, decision) == purged
    checks["purged_unavailable"] = runtime.read(ordinary, erased_id) is None
    checks["purged_search_empty"] = runtime.search(ordinary, "橙色纸船", mode="exact").hits == ()
    _, input_id = save("合成流请求。")
    stream = Event(Source("installed-control", "synthetic", "main", "stream"), "assistant", 100, None, "partial", "合成已确认首块🙂。")
    ack = runtime.append_stream(owner, stream, index=0, carry="", input_id=input_id)
    checks["stream_first_committed"] = runtime.read_stream(ordinary, stream.source).text == stream.text[:ack.released_chars]
    runtime.append_stream(owner, Event(stream.source, "assistant", 100, None, "complete", "合成终块。"), index=1, carry="", input_id=input_id)
    full = stream.text + "合成终块。"
    checks["stream_final_exact"] = runtime.read_stream(ordinary, stream.source).text == full
    updates = PreferenceUpdates(runtime)
    processor = updates.settings(owner).rules_processor
    updates.configure(owner, enabled=True, admitted_processor=processor)
    runtime.export_archive(owner, root / "old-admitted")
    previous = capture_control_checkpoint(runtime, owner)
    updates.configure(owner, enabled=True, admitted_processor=None)
    checkpoint = capture_control_checkpoint(runtime, owner)
    checks["admission_revocation_stales_checkpoint"] = previous.control_head != checkpoint.control_head
    replay_archive_controls(runtime, owner, root / "old-admitted", root / "old-review", checkpoint=checkpoint)
    try:
        publish_replayed_runtime(runtime, owner, root / "old-review", root / "forbidden-runtime",
                                 checkpoint=checkpoint, client_id="bad", entry="installed-control")
        checks["revoked_admission_not_restored"] = False
    except MemoryFailure as failure:
        checks["revoked_admission_not_restored"] = failure.code == "archive_evidence_incomplete"
    runtime.export_archive(owner, root / "current")
    replay_archive_controls(runtime, owner, root / "current", root / "review", checkpoint=checkpoint)
    restored_config, restored_owner = publish_replayed_runtime(runtime, owner, root / "review", root / "restored",
        checkpoint=checkpoint, client_id="synthetic-restored", entry="installed-control")
    restored = MemoryRuntime(restored_config)
    restored_original = restored.read(restored_owner, original_id)
    checks["restored_original_exact"] = restored_original is not None and restored_original.event.text == original
    checks["restored_important_exact"] = restored.read_important(restored_owner, important.important_id).text == original
    checks["restored_suppression"] = restored.read(restored_owner, hidden_id) is None
    checks["restored_purge"] = restored.read(restored_owner, erased_id) is None
    checks["restored_stream_exact"] = restored.read_stream(restored_owner, stream.source).text == full
    checks["restored_revocation"] = PreferenceUpdates(restored).settings(restored_owner).admitted_processor is None
    checks["restored_correction"] = FactRegistry(restored).query(restored_owner, old.subject_id, old.predicate,
        valid_at="2021-01-01T00:00:00Z").fact == corrected
    try:
        restored.submit(restored_owner, erased_event)
        checks["purged_source_no_resurrection"] = False
    except MemoryFailure as failure:
        checks["purged_source_no_resurrection"] = failure.code == "source_purged"
    report = {"version": importlib.metadata.version("evernook"), "import_file": evernook.__file__, "checks": checks,
        "purge_receipt": asdict(purged), "scope": "Installed wheel public API explicit facts/controls and independent runtime publication. No automatic extraction, native model quality, physical sleep or power-loss claim."}
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    raise SystemExit(0 if all(checks.values()) else 1)


if __name__ == "__main__":
    main()
