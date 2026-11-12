"""Finite low-risk first-person clauses with full-context scope guards; no model inference."""
from __future__ import annotations

from dataclasses import dataclass
import re

from .preferences import _category


@dataclass(frozen=True)
class PreferenceStatement:
    start: int
    end: int
    text: str
    normalized: str
    category: str
    scope: str
    negative: bool


@dataclass(frozen=True)
class PreferenceAssessment:
    statements: tuple[PreferenceStatement, ...]
    blocked_reason: str | None = None


_CONTEXT = re.compile(r'[“”「」『』"\']|同事|(?:朋友|他|她|别人|某人)(?:说|表示|认为)|原话|引用|转述|扮演|举例|示例|例子|假如|假设|如果|要是|'
                      r'推测|猜测|据说|谎称|假装|不确定|也许|可能|不代表|不是说|没有说|没说|不要记|别记|测试|不是真的')
_HIGH_RISK = re.compile(r'病史|疾病|诊断|病情|治疗|药物|药品|剂量|预算|收入|存款|负债|账户|账号|口令|密码|'
                        r'我的伴侣|我叫|我的名字|身份证|住址|政治|宗教|免确认|执行规则')
_TEMPORARY = re.compile(r'这次|本次|本轮|这轮|这一轮|仅本|只限|暂时|临时|今天')
_PAST = re.compile(r'以前|过去|曾经|当时|从前|上次|之前|先前')
_FUTURE = re.compile(r'明年|将来|未来|打算|计划|准备')
_CURRENT = re.compile(r'现在|目前|如今|现今|平时|一般|通常|一直|长期')
_DIRECT_PREFIX = re.compile(r'(?:(?:但|不过|而|现在|目前|如今|现今|以前|过去|曾经|当时|从前|上次|之前|先前|'
                            r'明确|确认|其实|确实|一般|通常|一直|长期|平时|往往|暂时|临时|今天|这次|本次|本轮|这轮|这一轮|从|明年|开始)|\s)*')
_CORRECTION = re.compile(r'纠正|更正|口误|附和|说错|不属实')
_CLAUSE = re.compile(r'我(?:(?:一般|通常|一直|长期|平时|往往|其实|确实))?(?:并不|不再|不)?'
                     r'(?:偏好用|偏好|喜欢|爱好|常用|习惯用|希望)[^，,。；;\n！？!?]{1,96}[。！!]?')


def normalize_statement(text: str) -> str:
    value = re.sub(r'^我(?:一般|通常|一直|长期|平时|往往|其实|确实)', '我', text.strip())
    value = re.sub(r'^我并不', '我不', value)
    value = re.sub(r'(?<!简)短(?=(?:的)?(?:回答|回复|答复))', '简短', value)
    return re.sub(r'\s+([。.!！])$', r'\1', value)


def statement_category(text: str) -> tuple[str | None, bool, str]:
    normalized = normalize_statement(text)
    positive = re.sub(r'^我(?:不再|不)', '我', normalized)
    return _category(positive), positive != normalized, positive


def assess_preferences(text: str) -> PreferenceAssessment:
    if len(text) > 16384:
        return PreferenceAssessment((), "input_too_large")
    if _CONTEXT.search(text):
        return PreferenceAssessment((), "quoted_conditional_or_uncertain_context")
    if _HIGH_RISK.search(text):
        return PreferenceAssessment((), "high_risk_context")
    statements = []
    for match in _CLAUSE.finditer(text):
        clause = match[0].rstrip()
        category, negative, _ = statement_category(clause)
        if category is None:
            continue
        start, end = match.start(), match.start() + len(clause)
        if end < len(text) and text[end] in "？?":
            continue
        # Commas delimit the local temporal prefix, so a past clause does not date the next one.
        boundary = max((text.rfind(mark, 0, start) for mark in "，,。；;\n！？!?"), default=-1)
        prefix = text[boundary + 1:start]
        if len(prefix) > 64 or _DIRECT_PREFIX.fullmatch(prefix) is None:
            continue
        sentence_boundary = max((text.rfind(mark, 0, start) for mark in "。；;\n！？!?"), default=-1)
        temporal_prefix = prefix if _CURRENT.search(prefix + clause) else text[sentence_boundary + 1:start]
        sentence_end = next((index for index in range(end, len(text)) if text[index] in "。；;\n！？!?"), len(text))
        suffix = text[end:sentence_end]
        restricted = bool(re.search(r'只对这次有效|仅本次有效|只是这次|只限这次|只对本次', suffix))
        scope = ("turn" if _TEMPORARY.search(temporal_prefix + clause) or restricted else "planned" if _FUTURE.search(temporal_prefix)
                 else "historical" if _PAST.search(temporal_prefix) else "long_term")
        if negative and scope == "long_term" and _CORRECTION.search(text) and re.search(r'现在确认|确认并不|确认不再', text):
            scope = "withdrawal"
        statements.append(PreferenceStatement(start, end, clause, normalize_statement(clause), category, scope, negative))
    # Unsupported wording is not evidence that a later denial is absent.
    for denial in re.finditer(r'我[^，,。；;\n！？!?]{0,12}(?:并不|不再|不)(?:偏好|喜欢|爱好|常用|习惯用)', text):
        if not any(item.start <= denial.start() and denial.end() <= item.end for item in statements):
            return PreferenceAssessment((), 'unparsed_negative_context')
    # Never silently accept an early positive while a later denial falls outside an output cap.
    if len(statements) > 32:
        return PreferenceAssessment((), "too_many_statements")
    return PreferenceAssessment(tuple(statements))


def match_statement(text: str, start: int, end: int, clause: str, category: str) -> PreferenceStatement | None:
    return next((statement for statement in assess_preferences(text).statements
                 if (statement.start, statement.end, statement.text, statement.category) == (start, end, clause, category)), None)
