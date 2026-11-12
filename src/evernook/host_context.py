"""Trusted host settings for the shared context builder and target tokenizer."""
from __future__ import annotations

from dataclasses import asdict, replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from typing import Any

from .context import ContextBuilder, ContextBundle, ContextPolicy
from .models import Access, MemoryFailure
from .paths import external
from .preset import COMPANION_PRESET
from .runtime import MemoryRuntime, _load
from .semantic import SemanticRecall
from .tokenizer import LocalTokenCounter


class HostContext:
    def __init__(self, runtime: MemoryRuntime, recall: SemanticRecall, config_path: Path):
        self.runtime, self.recall, self.config_path = runtime, recall, external(config_path)

    def _configured(self) -> tuple[ContextPolicy, LocalTokenCounter]:
        config = _load(self.config_path)
        if (set(config) != {"schema_version", "tokenizer_config", "policy"}
                or type(config["schema_version"]) is not int or config["schema_version"] != 1
                or not isinstance(config["tokenizer_config"], str) or not isinstance(config["policy"], dict)):
            raise MemoryFailure("invalid_host_context_config")
        policy = dict(config["policy"])
        try:
            for name in ("pinned_ids", "experience_ids", "fact_keys"):
                if name in policy:
                    if not isinstance(policy[name], list):
                        raise MemoryFailure("invalid_context_policy")
                    if name == "fact_keys" and any(not isinstance(key, list) or len(key) != 2
                            or any(not isinstance(part, str) for part in key) for key in policy[name]):
                        raise MemoryFailure("invalid_context_policy")
                    policy[name] = tuple(tuple(key) for key in policy[name]) if name == "fact_keys" else tuple(policy[name])
            selected = ContextPolicy(**policy)
        except (TypeError, ValueError):
            raise MemoryFailure("invalid_context_policy") from None
        counter = LocalTokenCounter.from_file(Path(config["tokenizer_config"]))
        ContextBuilder(self.runtime, self.recall, selected, counter)
        return selected, counter

    def status(self) -> dict[str, Any]:
        policy, counter = self._configured()
        recommended = {"normal_tokens": COMPANION_PRESET["session_tokens"], "deep_tokens": COMPANION_PRESET["deep_tokens"],
            "pinned_tokens": COMPANION_PRESET["must_read_tokens"], "persona_tokens": COMPANION_PRESET["persona_tokens"],
            "profile_tokens": COMPANION_PRESET["profile_tokens"], "recent_days": COMPANION_PRESET["recent_days"],
            "recent_target_chars": COMPANION_PRESET["recent_target_chars"]}
        return {"configured": True, "model_id": counter.model_id, "counter_id": counter.counter_id,
                "saved_policy": asdict(policy), "recommended_policy": recommended}

    def build(self, access: Access, query: str, *, model_id: str, max_tokens: int,
              as_of: str, mode: str, timeout: float, cancel: threading.Event | None = None,
              current_input_id: str | None = None, query_intent: str | None = None) -> ContextBundle:
        self.runtime._require_scope(access, "recall", "host_context_build")
        policy, counter = self._configured()
        counter.cancel = cancel
        if model_id != counter.model_id:
            raise MemoryFailure("tokenizer_model_mismatch")
        if type(max_tokens) is not int or not 128 <= max_tokens <= 131072:
            raise MemoryFailure("invalid_context_request")
        effective = replace(policy, normal_tokens=min(policy.normal_tokens, max_tokens),
            deep_tokens=min(policy.deep_tokens, max_tokens), pinned_tokens=min(policy.pinned_tokens, max_tokens))
        with counter.session(timeout=timeout) as scoped:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="evernook-tokenizer-warmup") as warmup:
                # Initialize this request's worker while recall runs; real counts still
                # recheck the assets. No query or source text is sent by the warmup.
                ready = warmup.submit(scoped.count, "")
                try:
                    bundle = ContextBuilder(self.runtime, self.recall, effective, scoped).build(
                        access, query, as_of=as_of, mode=mode, timeout=timeout,
                        cancel=cancel, current_input_id=current_input_id, query_intent=query_intent)
                    ready.result()
                    return bundle
                finally:
                    # Also unblock a pending warmup before joining it on early rejection.
                    scoped.close()
