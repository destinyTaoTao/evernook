"""Explicit, temporary, bounded memory exploration. Model output never gains write authority."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict, dataclass, field
import copy
import hashlib
import math
import threading
import time
from typing import Any, Callable, Protocol, runtime_checkable

from .models import Access, MemoryFailure
from .query_plan import plan_query
from .reflection_evidence import EvidenceTools
from .runtime import MemoryRuntime, _identifier, _json
from .safety import credential_signal
from .semantic import SemanticRecall
from .topic_views import TopicViews

_REMOTE_EGRESS = object()


@dataclass(frozen=True)
class ReflectionBudget:
    seconds: float = 60
    search_rounds: int = 4
    model_rounds: int = 4
    tool_calls: int = 8
    candidates: int = 32
    sources: int = 16
    source_chars: int = 8192
    input_tokens: int = 8192
    output_tokens: int = 1024


@dataclass(frozen=True)
class ReflectionPolicy:
    normal: ReflectionBudget = field(default_factory=lambda: ReflectionBudget(20, 1, 1, 2, 10, 8, 4096, 4096, 256))
    deep: ReflectionBudget = field(default_factory=ReflectionBudget)
    concurrency: int = 1
    cache_entries: int = 0
    cache_ttl: int = 30


@dataclass(frozen=True)
class ModelTurn:
    action: dict[str, Any]
    input_tokens: int
    output_tokens: int
    cost_micro_usd: int = 0
    error: str | None = None


class ReflectionModel(Protocol):
    processor_version: str
    def turn(self, payload: dict[str, Any], *, input_tokens: int, output_tokens: int,
             timeout: float, cancel: threading.Event | None) -> ModelTurn: ...


@runtime_checkable
class RemoteModel(ReflectionModel, Protocol):
    allowed_clients: frozenset[str]
    max_cost_micro_usd: int
    turn_input_tokens: int
    turn_output_tokens: int
    def reserve_cost(self, input_tokens: int, output_tokens: int) -> int: ...


class ReflectionEngine:
    def __init__(self, runtime: MemoryRuntime, recall: SemanticRecall, model: ReflectionModel | None,
                 policy: ReflectionPolicy = ReflectionPolicy()):
        if not isinstance(policy, ReflectionPolicy):
            raise MemoryFailure('invalid_reflection_policy')
        for budget in (policy.normal, policy.deep):
            if not isinstance(budget, ReflectionBudget) or type(budget.seconds) not in (int, float) or not math.isfinite(budget.seconds) or not 1 <= budget.seconds <= 120:
                raise MemoryFailure('invalid_reflection_policy')
            for name, maximum in (('search_rounds', 8), ('model_rounds', 8), ('tool_calls', 16), ('candidates', 100),
                    ('sources', 32), ('source_chars', 16384), ('input_tokens', 32768), ('output_tokens', 4096)):
                value = getattr(budget, name)
                if type(value) is not int or not 1 <= value <= maximum:
                    raise MemoryFailure('invalid_reflection_policy')
        if (type(policy.concurrency) is not int or not 1 <= policy.concurrency <= 2 or type(policy.cache_entries) is not int
                or not 0 <= policy.cache_entries <= 32 or type(policy.cache_ttl) is not int or not 1 <= policy.cache_ttl <= 300):
            raise MemoryFailure('invalid_reflection_policy')
        self.runtime, self.recall, self.model, self.policy = runtime, recall, model, policy
        self.identity = hashlib.sha256(_json([TopicViews(runtime, recall).processor, asdict(policy), model.processor_version if model else None]).encode()).hexdigest()
        self.slots = threading.BoundedSemaphore(policy.concurrency)
        self.lock = threading.Lock()
        self.cache: OrderedDict[str, tuple[float, dict[str, Any], EvidenceTools]] = OrderedDict()

    def configure(self, access: Access) -> dict[str, Any]:
        self.runtime._require_scope(access, 'maintain', 'reflection_configure')
        self.runtime._require_scope(access, 'accept', 'reflection_configure')
        with self.runtime._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            TopicViews._schema(db)
            db.execute('COMMIT')
        return {'enabled': True, 'processor': self.identity, 'model_configured': self.model is not None,
            'provider': 'remote' if isinstance(self.model, RemoteModel) else 'local',
            'policy': asdict(self.policy), 'model_admission': 'experimental', 'publication': 'temporary_only'}

    def clock(self, check: Any) -> dict[str, Any]:
        with self.runtime._connection(check=check) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='topic_view_clock_v1'").fetchone():
                raise MemoryFailure('reflection_not_configured')
            return TopicViews._clock(db)

    @staticmethod
    def _done(action: dict[str, Any], tools: EvidenceTools) -> dict[str, Any]:
        fields = {'action', 'conclusions', 'conflicts', 'unknown', 'clarification_required'}
        if set(action) not in (fields, fields | {'assessment'}) or type(action['clarification_required']) is not bool:
            raise MemoryFailure('reflection_invalid_action')
        ids = {item['evidence_id'] for item in tools.evidence}
        def bind_support(rows: object) -> list[dict[str, Any]]:
            from .reflection_protocol import support_spans
            if (not isinstance(rows, list) or len(rows) > 16 or any(not isinstance(row, dict)
                    or set(row) != {'evidence_id', 'quote'} or not isinstance(row['evidence_id'], str) for row in rows)):
                raise MemoryFailure('reflection_invalid_support')
            quotes = {row['evidence_id']: row['quote'] for row in rows}
            if len(quotes) != len(rows):
                raise MemoryFailure('reflection_invalid_support')
            checked = support_spans(quotes, tools.evidence)
            sources = {item['evidence_id']: item for item in tools.evidence}
            bound = []
            for row in checked:
                source = sources[row['evidence_id']]
                start = source['start'] + source['quote'].index(row['quote'])
                bound.append({**row, 'message_id': source['message_id'], 'state_version': source['state_version'],
                    'content_version': source['content_version'], 'start': start, 'end': start + len(row['quote'])})
            return bound
        for name in ('conclusions', 'conflicts', 'unknown'):
            if not isinstance(action[name], list) or len(action[name]) > 16:
                raise MemoryFailure('reflection_invalid_action')
        conclusions = []
        for value in action['conclusions']:
            if (not isinstance(value, dict) or set(value) not in ({'text', 'evidence_ids', 'inference'}, {'text', 'evidence_ids', 'inference', 'support'}) or value['inference'] is not True
                    or not isinstance(value['text'], str) or not 1 <= len(value['text']) <= 512 or credential_signal(value['text'])
                    or not isinstance(value['evidence_ids'], list) or not 1 <= len(value['evidence_ids']) <= 16
                    or any(not isinstance(item, str) or item not in ids for item in value['evidence_ids'])):
                raise MemoryFailure('reflection_invalid_citation')
            conclusion = {**value, 'verification': 'unverified_model_inference'}
            if 'support' in value:
                support = bind_support(value['support'])
                if {row['evidence_id'] for row in support} != set(value['evidence_ids']):
                    raise MemoryFailure('reflection_invalid_support')
                conclusion['support'] = support
            conclusions.append(conclusion)
        for conflict in action['conflicts']:
            if not isinstance(conflict, list) or any(not isinstance(item, str) or item not in ids for item in conflict) or len(set(conflict)) < 2:
                raise MemoryFailure('reflection_invalid_citation')
        if any(not isinstance(value, str) or not 1 <= len(value) <= 512 or credential_signal(value) for value in action['unknown']):
            raise MemoryFailure('reflection_invalid_action')
        if not tools.evidence and conclusions:
            raise MemoryFailure('reflection_invalid_citation')
        assessment = None
        if 'assessment' in action:
            assessment = action['assessment']
            if (not isinstance(assessment, dict) or set(assessment) != {'decision', 'support', 'origin'}
                    or not isinstance(assessment['decision'], str) or not isinstance(assessment['origin'], str)
                    or assessment['decision'] not in {'answer', 'abstain', 'clarify', 'refuse'}
                    or assessment['origin'] not in {'model', 'finite_preference_guard', 'explicit_year_guard'}):
                raise MemoryFailure('reflection_invalid_action')
            bound = bind_support(assessment['support'])
            decision = assessment['decision']
            if (decision == 'answer' and (not conclusions or action['conflicts'] or action['clarification_required'])
                    or decision == 'clarify' and (conclusions or not action['conflicts'] or not action['clarification_required'])
                    or decision in {'abstain', 'refuse'} and (conclusions or action['conflicts'])):
                raise MemoryFailure('reflection_invalid_action')
            selected = {row['evidence_id'] for row in bound}
            expected = ({ref for item in conclusions for ref in item['evidence_ids']} if decision == 'answer'
                else {ref for group in action['conflicts'] for ref in group} if decision == 'clarify' else selected)
            if selected != expected or decision in {'answer', 'clarify', 'refuse'} and not selected:
                raise MemoryFailure('reflection_invalid_support')
            assessment = {**assessment, 'support': bound}
        return {'conclusions': conclusions, 'conflicts': action['conflicts'], 'unknown': action['unknown'],
            'clarification_required': action['clarification_required'] or bool(action['conflicts']), 'assessment': assessment}

    def reflect(self, access: Access, query: str, *, mode: str = 'deep', topic_view_ids: tuple[str, ...] = (),
                timeout: float | None = None, cancel: threading.Event | None = None, current_input_id: str | None = None,
                allow_remote: bool = False, provider_check: Callable[[], None] | None = None) -> dict[str, Any]:
        self.runtime._require_scope(access, 'recall', 'reflection')
        if not isinstance(mode, str) or mode not in {'normal', 'deep'} or not isinstance(query, str) or not 1 <= len(query) <= 1048576:
            raise MemoryFailure('invalid_reflection_request')
        if not isinstance(topic_view_ids, tuple) or len(topic_view_ids) > 8 or type(allow_remote) is not bool:
            raise MemoryFailure('invalid_reflection_request')
        for view_id in topic_view_ids:
            _identifier(view_id)
        if current_input_id is not None:
            _identifier(current_input_id)
        selected = self.policy.normal if mode == 'normal' else self.policy.deep
        if timeout is not None and (type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 120):
            raise MemoryFailure('invalid_reflection_request')
        started = time.monotonic()
        total = min(selected.seconds, timeout) if timeout is not None else selected.seconds
        deadline = started + total
        remote = self.model if isinstance(self.model, RemoteModel) else None
        usage: dict[str, Any] = {'search_rounds': 0, 'model_rounds': 0, 'tool_calls': 0, 'input_tokens': 0, 'output_tokens': 0,
            'cost_reserved_micro_usd': 0, 'cost_reported_micro_usd': 0}
        result: dict[str, Any] = {'kind': 'untrusted_memory_reflection', 'state': 'partial', 'mode': mode, 'processor': self.identity,
            'model_admission': 'experimental', 'conclusions': [], 'conflicts': [], 'unknown': ['Memory coverage and semantic support are not established.'],
            'clarification_required': True, 'assessment': None, 'evidence': [], 'coverage': [], 'degraded': [], 'usage': usage,
            'budget': asdict(selected), 'cached': False, 'publication': 'temporary_only'}
        result['provider'] = 'remote' if remote is not None else 'local'
        if credential_signal(query):
            result.update(state='protected_route_required', stop_reason='protected_route_required')
            return result
        if remote is not None and (not allow_remote or access.client_id not in remote.allowed_clients):
            raise MemoryFailure('remote_reflection_not_permitted')
        plan = plan_query(query)
        if plan.limited:
            result['degraded'].append('query_coverage_limited')
        def check(*, final: bool = False) -> float:
            if not final and cancel is not None and cancel.is_set():
                raise MemoryFailure('reflection_canceled')
            left = deadline - time.monotonic() - (0 if final else min(.5, total / 10))
            if left <= 0:
                raise MemoryFailure('reflection_timeout')
            return left
        # Precancellation must not touch indexes or start a model worker.
        check()
        if remote is not None:
            EvidenceTools.check_remote_input(self.runtime, access, current_input_id, check)
        if not self.slots.acquire(blocking=False):
            result.update(state='busy', stop_reason='reflection_busy')
            return result
        tools = EvidenceTools(self.runtime, self.recall, access, maximum=selected.sources, chars=selected.source_chars,
            candidates=selected.candidates, check=check, current_input_id=current_input_id)
        clock: dict[str, Any] | None = None
        key = ''
        completed = False
        try:
            clock = self.clock(check)
            key = hashlib.sha256(_json([access.client_id, self.identity, query, mode, topic_view_ids, current_input_id, clock]).encode()).hexdigest()
            with self.lock:
                cached = self.cache.get(key)
            if cached is not None and cached[0] > time.monotonic():
                _, previous, old_tools = cached
                if old_tools.verify(lambda: check(final=True)) and self.clock(lambda: check(final=True)) == clock:
                    self.runtime._require_scope(access, 'recall', 'reflection_cached_handoff')
                    answer = copy.deepcopy(previous)
                    answer['cached'] = True
                    answer['usage'] = {**{name: 0 for name in usage}, 'elapsed_ms': (time.monotonic() - started) * 1000}
                    return answer
            for view_id in topic_view_ids:
                if usage['tool_calls'] >= selected.tool_calls:
                    raise MemoryFailure('reflection_tool_budget')
                usage['tool_calls'] += 1
                tools.view(view_id)
            # Always explore original evidence before accepting unknown/done from a model.
            usage['search_rounds'] += 1
            usage['tool_calls'] += 1
            if usage['tool_calls'] > selected.tool_calls:
                raise MemoryFailure('reflection_tool_budget')
            tools.search(plan.query, cancel=cancel)
            if self.model is None:
                raise MemoryFailure('reflection_model_unconfigured')
            while usage['model_rounds'] < selected.model_rounds:
                check()
                self.runtime._require_scope(access, 'recall', 'reflection_model_input')
                if self.clock(check) != clock or not tools.verify(check):
                    raise MemoryFailure('memory_changed_during_reflection')
                left_input = selected.input_tokens - usage['input_tokens']
                left_output = selected.output_tokens - usage['output_tokens']
                if left_input <= 0 or left_output <= 0:
                    raise MemoryFailure('reflection_token_budget')
                input_cap = min(left_input, remote.turn_input_tokens if remote is not None else 4096)
                output_cap = min(left_output, remote.turn_output_tokens if remote is not None else 512)
                payload = {'question': query, 'query_plan': asdict(plan), 'evidence': copy.deepcopy(tools.evidence),
                    'coverage': copy.deepcopy(tools.coverage), 'round': usage['model_rounds'] + 1,
                    'allowed_view_ids': list(topic_view_ids)}
                if usage.get('rejected_turns'):
                    payload['format_feedback'] = usage['rejected_turns'][-1]
                if remote is not None:
                    tools.authorize_remote()
                    payload['_remote_egress'] = _REMOTE_EGRESS
                    reserve = remote.reserve_cost(input_cap, output_cap)
                    if type(reserve) is not int or reserve < 0 or usage['cost_reserved_micro_usd'] + reserve > remote.max_cost_micro_usd:
                        raise MemoryFailure('reflection_cost_budget')
                    usage['cost_reserved_micro_usd'] += reserve
                if provider_check is not None:
                    provider_check()
                usage['model_rounds'] += 1
                usage['metering_incomplete'] = True
                turn = self.model.turn(payload, input_tokens=input_cap, output_tokens=output_cap, timeout=check(), cancel=cancel)
                if (not isinstance(turn, ModelTurn) or any(type(value) is not int or value < 0 for value in (turn.input_tokens, turn.output_tokens))
                        or turn.input_tokens > input_cap or turn.output_tokens > output_cap
                        or type(turn.cost_micro_usd) is not int or turn.cost_micro_usd < 0):
                    raise MemoryFailure('reflection_token_budget')
                usage['input_tokens'] += turn.input_tokens
                usage['output_tokens'] += turn.output_tokens
                usage['cost_reported_micro_usd'] += turn.cost_micro_usd
                usage['metering_incomplete'] = False
                check()
                if self.clock(check) != clock:
                    raise MemoryFailure('memory_changed_during_reflection')
                if turn.error is not None:
                    raise MemoryFailure('reflection_remote_invalid_output')
                action = turn.action
                if not isinstance(action, dict) or not isinstance(action.get('action'), str):
                    raise MemoryFailure('reflection_invalid_action')
                name = action['action']
                if name == 'model_retry' and set(action) == {'action', 'reason'} and isinstance(action['reason'], str) and action['reason'] in {'invalid_json', 'invalid_action', 'invalid_support'}:
                    usage.setdefault('rejected_turns', []).append(action['reason'])
                    continue
                if name == 'done':
                    result.update(self._done(action, tools))
                    completed = True
                    break
                if remote is not None:
                    raise MemoryFailure('reflection_remote_evidence_scope')
                if usage['tool_calls'] >= selected.tool_calls:
                    raise MemoryFailure('reflection_tool_budget')
                usage['tool_calls'] += 1
                if name == 'search' and set(action) == {'action', 'query'}:
                    if usage['search_rounds'] >= selected.search_rounds:
                        raise MemoryFailure('reflection_search_budget')
                    if not isinstance(action['query'], str) or not 1 <= len(action['query']) <= 256 or credential_signal(action['query']):
                        raise MemoryFailure('reflection_invalid_action')
                    usage['search_rounds'] += 1
                    tools.search(action['query'], cancel=cancel)
                elif name == 'read' and set(action) == {'action', 'message_id', 'start', 'chars'} and isinstance(action['message_id'], str):
                    tools.admit(action['message_id'], action['start'], action['chars'])
                elif name == 'navigation' and set(action) == {'action', 'message_id'} and isinstance(action['message_id'], str):
                    tools.navigation(action['message_id'])
                elif name == 'view' and set(action) == {'action', 'view_id'} and action['view_id'] in topic_view_ids:
                    tools.view(action['view_id'])
                else:
                    raise MemoryFailure('reflection_invalid_action')
            if not completed:
                raise MemoryFailure('reflection_model_round_budget')
            result.update(state='complete', stop_reason='done')
        except MemoryFailure as error:
            if error.code in {'unauthorized', 'permission_denied', 'incompatible_contract', 'service_required'}:
                raise
            result['degraded'].append(error.code)
            result['stop_reason'] = error.code
            result.update(conclusions=[], conflicts=[], assessment=None, unknown=['Memory coverage and semantic support are not established.'], clarification_required=True)
        finally:
            self.slots.release()
        try:
            if remote is not None:
                EvidenceTools.check_remote_input(self.runtime, access, current_input_id, lambda: check(final=True))
            if not tools.verify(lambda: check(final=True)) or clock is not None and self.clock(lambda: check(final=True)) != clock:
                result['degraded'].append('memory_changed_during_reflection')
                result.update(state='partial', stop_reason='memory_changed_during_reflection', conclusions=[], conflicts=[], assessment=None,
                    unknown=['Memory coverage and semantic support are not established.'], clarification_required=True)
        except MemoryFailure as error:
            if error.code in {'unauthorized', 'permission_denied', 'incompatible_contract', 'service_required'}:
                raise
            tools.evidence = []
            result['degraded'].append(error.code)
            result.update(state='partial', stop_reason=error.code, conclusions=[], conflicts=[], assessment=None,
                unknown=['Memory coverage and semantic support are not established.'], clarification_required=True)
        result['evidence'] = copy.deepcopy(tools.evidence)
        result['coverage'] = tools.coverage
        usage.update(candidates=tools.candidate_count, source_spans=len(tools.evidence), source_chars=tools.returned_chars,
            elapsed_ms=(time.monotonic() - started) * 1000)
        result['degraded'] = list(dict.fromkeys(result['degraded']))
        if self.policy.cache_entries and result['state'] == 'complete' and not result['degraded']:
            with self.lock:
                self.cache[key] = (time.monotonic() + self.policy.cache_ttl, copy.deepcopy(result), tools)
                while len(self.cache) > self.policy.cache_entries:
                    self.cache.popitem(last=False)
        self.runtime._require_scope(access, 'recall', 'reflection_final_handoff')
        return result
