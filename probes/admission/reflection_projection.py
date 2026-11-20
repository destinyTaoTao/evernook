"""Project actual reflection decisions without consulting frozen expected answers."""
from __future__ import annotations

from typing import Any


def project_result(result: dict[str, Any], aliases: dict[str, str]) -> dict[str, Any]:
    state = result['state']
    if state == 'protected_route_required':
        return {'judgment': {'decision': 'refuse', 'value': None, 'source_ids': []}}
    cited = result['evidence']
    payload: dict[str, Any] = {}
    # Earlier search coverage persists even when final verification discards every citation.
    if cited or state == 'complete':
        payload['retrieval'] = {'source_ids': list(dict.fromkeys(aliases[item['message_id']] for item in cited))}
        payload['evidence'] = {'citations': [{'source_id': aliases[item['message_id']], **{key: item[key]
            for key in ('start', 'end', 'quote', 'state_version', 'content_version')}} for item in cited]}
    assessment = result.get('assessment')
    if state == 'complete' and assessment is not None:
        by_id = {item['evidence_id']: aliases[item['message_id']] for item in cited}
        payload['judgment'] = {'decision': assessment['decision'],
            'value': '\n'.join(item['text'] for item in result['conclusions']) or None,
            'source_ids': sorted({by_id[item['evidence_id']] for item in assessment['support']})}
    return payload
