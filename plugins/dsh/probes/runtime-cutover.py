"""Synthetic-only installed-wheel cutover fixture. Never switch a user profile or print credentials."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys
import zipfile

import evernook
from evernook import Access, Event, ImportantMark, MemoryFailure, MemoryRuntime, Source, pair_client
from evernook.archive import capture_control_checkpoint, replay_archive_controls
from evernook.management import MemoryManagement
from evernook.purge import PurgeDecision, PurgeWorkflow
from evernook.runtime_restore import publish_replayed_runtime


def save(path: Path, value: object) -> None:
    with path.open('x', encoding='utf-8') as output:
        json.dump(value, output, ensure_ascii=False, indent=2)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def frozen(root: Path) -> dict[str, str]:
    return {path.name: digest(path) for path in root.iterdir() if path.is_file()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('identity', 'prepare', 'publish', 'verify'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--checkout', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    checkout = args.checkout.resolve(strict=True)
    if not root.name.startswith('evernook-publish94-') or root.is_relative_to(checkout):
        raise ValueError('external_synthetic_root_required')
    stage = root / 'cutover-stage'
    if args.phase == 'prepare' and (stage / 'fixture.json').exists():
        raise ValueError('synthetic_fixture_already_prepared')
    if args.phase == 'publish' and ((stage / 'restored').exists() or (stage / 'source-frozen.json').exists()):
        raise ValueError('cutover_already_published_do_not_reopen_source')
    installed = Path(evernook.__file__).resolve().parent
    if 'site-packages' not in installed.parts or installed.is_relative_to(checkout):
        raise ValueError('independent_installed_wheel_required')
    if args.phase == 'identity':
        wheel, = (stage / 'wheels').glob('evernook-*.whl')
        with zipfile.ZipFile(wheel) as archive:
            members = [name for name in archive.namelist() if name.startswith('evernook/') and name.endswith(('.py', '.mjs'))]
            assert set(members) == {'evernook/' + str(path.relative_to(checkout / 'src/evernook')).replace('\\', '/')
                for path in (checkout / 'src/evernook').rglob('*') if path.suffix in ('.py', '.mjs')}
            for name in members:
                assert archive.read(name) == (checkout / 'src' / name).read_bytes()
                assert archive.read(name) == (installed.parent / name).read_bytes()
        save(stage / 'identity.json', {'version': importlib.metadata.version('evernook'), 'python': sys.version,
            'installed': str(installed), 'wheel_sha256': digest(wheel), 'exact_package_files': len(members)})
        print(json.dumps({'installed_wheel': True, 'exact_package_files': len(members)}))
        return
    paths = json.loads((root / 'web-synthetic/synthetic-paths.json').read_text(encoding='utf-8'))
    source = Path(paths['config']).resolve(strict=True)
    if source != root / 'web-synthetic/memory/runtime.json':
        raise ValueError('unexpected_synthetic_source')
    if args.phase == 'prepare':
        runtime = MemoryRuntime(source)
        registry = json.loads((source.parent / 'client.json').read_text(encoding='utf-8'))
        owner = next(value for value in registry['clients'] if value['archive_review'] and 'admin' in value['scopes'])
        access = Access(owner['client_id'], owner['token'])
        ordinary = runtime.access_from_file(Path(paths['access']))
        maintenance = runtime.access_from_file(Path(paths['maintenanceAccess']))
        original = '合成切库重要原话：37.50元，🙂与e\u0301；保留\r\n换行。'
        event = Event(Source(owner['entry'], 'synthetic-cutover', 'main', 'original'), 'user', 0, None, 'complete', original)
        receipt = runtime.submit(access, event)
        important = runtime.mark_important(access, ImportantMark(receipt.message_id, 'original', 1, 'synthetic', None, None, original))
        management = MemoryManagement(runtime)
        canceled_operation = {'kind': 'source_suppress', 'message_id': receipt.message_id}
        canceled = management.preview(maintenance, canceled_operation)
        management.cancel(maintenance, canceled['proposal_id'])
        controlled = {}
        for kind in ('source_suppress', 'source_correct'):
            saved = runtime.submit(access, replace(event, source=replace(event.source, event=kind), text='合成旧值：' + kind))
            operation = {'kind': kind, 'message_id': saved.message_id}
            if kind == 'source_correct':
                operation['text'] = '合成更正值：已明确更新。'
            proposal = management.preview(maintenance, operation)
            controlled[kind] = management.confirm(maintenance, proposal['proposal_id'], operation)
        purged = runtime.submit(access, replace(event, source=replace(event.source, event='purge'), text='合成可删除试样'))
        purge = PurgeWorkflow(runtime)
        decision = purge.preview(access, 'synthetic-cutover-purge', (purged.message_id,))
        confirmation = runtime.submit(access, replace(event, source=replace(event.source, event='purge-confirmation'),
            text='合成确认：删除 synthetic-cutover-purge 指定试样。'))
        purge.purge(access, PurgeDecision(decision.request_id, confirmation.message_id))
        input_event = Event(Source('dsh', 'synthetic-cutover', 'main', 'input'), 'user', 0, None, 'complete', '合成可靠流跨库续接')
        input_receipt = runtime.submit(ordinary, input_event)
        stream = replace(input_event, source=replace(input_event.source, event='stream'), role='assistant', sequence=1,
            completion='partial', text='合成已确认前缀🙂。')
        runtime.append_stream(ordinary, stream, index=0, carry='', input_id=input_receipt.message_id)
        save(stage / 'fixture.json', {'event': asdict(event), 'receipt': asdict(receipt), 'important': important.important_id,
            'canceled': canceled['proposal_id'], 'canceled_operation': canceled_operation, 'controlled': controlled,
            'purged': purged.message_id, 'stream': asdict(stream), 'input_id': input_receipt.message_id,
            'ordinary_id': ordinary.client_id, 'maintenance_id': maintenance.client_id})
        print(json.dumps({'synthetic': True, 'prepared': True}))
        return
    fixture = json.loads((stage / 'fixture.json').read_text(encoding='utf-8'))
    target = stage / 'restored'
    if args.phase == 'publish':
        runtime = MemoryRuntime(source)
        registry = json.loads((source.parent / 'client.json').read_text(encoding='utf-8'))
        owner = next(value for value in registry['clients'] if value['archive_review'] and 'admin' in value['scopes'])
        access = Access(owner['client_id'], owner['token'])
        runtime.export_archive(access, stage / 'archive')
        checkpoint = capture_control_checkpoint(runtime, access)
        replay_archive_controls(runtime, access, stage / 'archive', stage / 'review', checkpoint=checkpoint)
        config, restored_owner = publish_replayed_runtime(runtime, access, stage / 'review', target, checkpoint=checkpoint,
            client_id='synthetic-cutover-owner', entry='cutover-owner')
        restored = MemoryRuntime(config)
        for old in (access, runtime.access_from_file(Path(paths['access'])), runtime.access_from_file(Path(paths['maintenanceAccess']))):
            try:
                restored.handshake(old)
            except MemoryFailure as error:
                assert error.code == 'unauthorized'
            else:
                raise AssertionError('old_token_admitted')
        pair_client(config, client_id=fixture['ordinary_id'], entry='dsh', access_path=target / 'mcp-access-dsh.json',
            scopes=('capture', 'recall'))
        pair_client(config, client_id=fixture['maintenance_id'], entry='management', access_path=target / 'mcp-access-management.json',
            scopes=('recall', 'maintain', 'accept'))
        # Preserve the new owner only inside the already private runtime directory.
        save(target / 'mcp-access-owner.json', asdict(restored_owner))
        save(stage / 'restored-paths.json', {**paths, 'config': str(config), 'access': str(target / 'mcp-access-dsh.json'),
            'maintenanceAccess': str(target / 'mcp-access-management.json')})
        save(stage / 'source-frozen.json', frozen(source.parent))
        print(json.dumps({'synthetic': True, 'published': True, 'old_tokens_rejected': 3}))
        return
    assert frozen(source.parent) == json.loads((stage / 'source-frozen.json').read_text(encoding='utf-8'))
    runtime = MemoryRuntime(target / 'runtime.json')
    ordinary = runtime.access_from_file(target / 'mcp-access-dsh.json')
    maintenance = runtime.access_from_file(target / 'mcp-access-management.json')
    assert runtime.handshake(ordinary)['scopes'] == ['capture', 'recall']
    assert runtime.handshake(maintenance)['scopes'] == ['accept', 'maintain', 'recall']
    for key in ('access', 'maintenanceAccess'):
        old_access = Access(**json.loads(Path(paths[key]).read_text(encoding='utf-8')))
        try:
            runtime.handshake(old_access)
        except MemoryFailure as error:
            assert error.code == 'unauthorized'
        else:
            raise AssertionError('old_token_admitted_after_repairing')
    stored_original = runtime.read(ordinary, fixture['receipt']['message_id'])
    assert stored_original is not None and stored_original.event.text == fixture['event']['text']
    assert runtime.read_important(ordinary, fixture['important']).text == fixture['event']['text']
    for outcome in fixture['controlled'].values():
        assert runtime.state(ordinary, outcome['target_id']).status == outcome['state']
        assert runtime.read(ordinary, outcome['target_id']) is None
    corrected = runtime.state(ordinary, fixture['controlled']['source_correct']['target_id'])
    assert corrected.replacement_id is not None
    replacement = runtime.read(ordinary, corrected.replacement_id)
    assert replacement is not None and replacement.event.text == '合成更正值：已明确更新。'
    assert runtime.read(ordinary, fixture['purged']) is None
    approval_source = Source(**{**fixture['event']['source'], 'event': 'purge-confirmation'})
    approval = runtime.read_source(ordinary, approval_source)
    assert approval is not None
    owner_access = runtime.access_from_file(target / 'mcp-access-owner.json')
    completed_purge = PurgeWorkflow(runtime).purge(owner_access,
        PurgeDecision('synthetic-cutover-purge', approval.receipt.message_id))
    assert completed_purge.state == 'completed' and completed_purge.source_ids == (fixture['purged'],)
    try:
        MemoryManagement(runtime).confirm(maintenance, fixture['canceled'], fixture['canceled_operation'])
    except MemoryFailure as error:
        assert error.code == 'management_preview_canceled'
    else:
        raise AssertionError('canceled_review_reactivated')
    stream_source = Source(**fixture['stream']['source'])
    page = runtime.read_stream(ordinary, stream_source)
    if page.state == 'open':
        event = Event(**{**fixture['stream'], 'source': stream_source, 'text': '合成切库后续块。', 'completion': 'complete'})
        runtime.append_stream(ordinary, event, index=1, carry=fixture['stream']['text'][len(page.text):], input_id=fixture['input_id'])
    page = runtime.read_stream(ordinary, stream_source)
    assert page.complete and page.text == fixture['stream']['text'] + '合成切库后续块。'
    checks = {'synthetic': True, 'old_source_frozen': True, 'exact_important_original': True,
        'source_controls': True, 'purged_unavailable': True, 'cancel_preserved': True, 'stream_continued': True}
    verification = stage / 'verified.json'
    if verification.exists():
        assert json.loads(verification.read_text(encoding='utf-8')) == checks
    else:
        save(verification, checks)
    print(json.dumps(checks))


if __name__ == '__main__':
    main()
