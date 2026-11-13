"""Synthetic-only wire experiments. No semantic repair or production configuration."""
from __future__ import annotations

import json
from typing import Any

from evernook.extraction import LocalQwen
from evernook.models import MemoryFailure
from evernook.reflection_protocol import build_messages, support_spans


def messages(payload: dict[str, Any], wire: str) -> list[dict[str, str]]:
    result = build_messages(payload)
    if wire == 'flat':
        return result
    if wire != 'refs':
        raise ValueError('invalid_candidate_wire')
    original = 'support是对象：键为当前输入的证据ID，值为从该证据quote逐字复制的支持原话字符串。不得改写引句、编造ID或复制示例答案。'
    replacement = 'support是数组，元素为当前输入的证据ID；程序绑定所选证据的原文。不得编造ID或复制示例答案。'
    if result[0]['content'].count(original) != 1:
        raise ValueError('unexpected_protocol_format')
    result[0]['content'] = result[0]['content'].replace(original, replacement).replace(
        '{"action":"abstain","support":{}}', '{"action":"abstain","support":[]}')
    for message in result:
        if message['role'] == 'assistant':
            response = json.loads(message['content'])
            response['support'] = list(response['support'])
            message['content'] = json.dumps(response, ensure_ascii=False, separators=(',', ':'))
    return result


def schema(payload: dict[str, Any], wire: str) -> dict[str, Any]:
    """Constrain shape/available references only; never inspect question, roles or gold."""
    refs = [item['evidence_id'] for item in payload['evidence']]
    reference: dict[str, Any] = {'type': 'string', 'enum': refs} if refs else {'type': 'string', 'maxLength': 0}
    if wire == 'refs':
        support: dict[str, Any] = {'type': 'array', 'items': reference, 'maxItems': min(16, len(refs))}
    elif wire == 'flat':
        support = {'type': 'object', 'properties': {ref: {'type': 'string'} for ref in refs}, 'additionalProperties': False}
    else:
        raise ValueError('invalid_candidate_wire')
    def obj(properties: dict[str, Any]) -> dict[str, Any]:
        return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}
    alternatives = [obj({'action': {'const': 'answer'}, 'value': {'type': 'string'}, 'support': support}),
        obj({'action': {'enum': ['abstain', 'clarify', 'refuse']}, 'support': support}),
        obj({'action': {'const': 'search'}, 'query': {'type': 'string'}}),
        obj({'action': {'const': 'read'}, 'message_id': {'type': 'string'}, 'start': {'type': 'integer'}, 'chars': {'type': 'integer'}}),
        obj({'action': {'const': 'navigation'}, 'message_id': {'type': 'string'}})]
    if payload.get('allowed_view_ids'):
        alternatives.append(obj({'action': {'const': 'view'}, 'view_id': {'enum': payload['allowed_view_ids']}}))
    return {'oneOf': alternatives}


def project(text: str, payload: dict[str, Any], wire: str) -> str:
    """Bind exactly chosen IDs without modifying the decision or answer string.

    This transports citations, not entailment. Missing hops remain missing;
    oversized sources fail rather than selecting a convenient shortened span.
    """
    if wire == 'flat':
        return text
    if wire != 'refs':
        raise ValueError('invalid_candidate_wire')
    action = LocalQwen.parse_selection(text)
    if not isinstance(action, dict):
        raise MemoryFailure('reflection_invalid_action')
    decision = action.get('action')
    if not isinstance(decision, str):
        raise MemoryFailure('reflection_invalid_action')
    if decision in {'search', 'read', 'navigation', 'view'}:
        return text
    fields = {'action', 'value', 'support'} if decision == 'answer' else {'action', 'support'}
    if (decision not in {'answer', 'abstain', 'clarify', 'refuse'} or set(action) != fields
            or decision == 'answer' and (not isinstance(action['value'], str) or not action['value'].strip())):
        raise MemoryFailure('reflection_invalid_action')
    chosen = action['support']
    available = {item['evidence_id']: item['quote'] for item in payload['evidence']}
    if (not isinstance(chosen, list) or len(chosen) > 16 or any(not isinstance(ref, str) or ref not in available for ref in chosen)
            or len(set(chosen)) != len(chosen)):
        raise MemoryFailure('reflection_invalid_support')
    bound = {ref: available[ref] for ref in chosen}
    support_spans(bound, payload['evidence'], required=decision != 'abstain')
    return json.dumps({**action, 'support': bound}, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
