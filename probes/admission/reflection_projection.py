"""Project actual reflection decisions without consulting frozen expected answers."""
from __future__ import annotations

from typing import Any


def project_result(result: dict[str, Any], aliases: dict[str, str]) -> dict[str, Any]:
    cited = result['evidence']
    by_id = {item['evidence_id']: aliases[item['message_id']] for item in cited}
    assessment = result.get('assessment')
    if assessment is not None:
        decision = assessment['decision']
        claimed = sorted({by_id[item['evidence_id']] for item in assessment['support']})
    else:
        decision = 'refuse' if result['state'] == 'protected_route_required' else (
            'clarify' if result['conflicts'] else 'answer' if result['conclusions'] and not result['clarification_required'] else 'abstain')
        claimed = sorted({by_id[ref] for item in result['conclusions'] for ref in item['evidence_ids']} or {
            by_id[ref] for group in result['conflicts'] for ref in group})
    return {'retrieval': {'source_ids': list(dict.fromkeys(aliases[item['message_id']] for item in cited))},
        'evidence': {'citations': [{'source_id': aliases[item['message_id']], **{key: item[key]
            for key in ('start', 'end', 'quote', 'state_version', 'content_version')}} for item in cited]},
        'judgment': {'decision': decision, 'value': '\n'.join(item['text'] for item in result['conclusions']) or None, 'source_ids': claimed}}
