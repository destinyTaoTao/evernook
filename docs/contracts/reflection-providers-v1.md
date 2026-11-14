# Explicit reflection providers (Core0.1.22)

Core 0.1.71 adds `model_identity` to the trusted management status for recognized, valid, unchanged service-bound providers. Local entries expose fixed built-in model labels and processor/vector-space digests; the remote entry exposes only the compatible provider type and a ledger-keyed opaque configuration digest, not its user-supplied model ID, endpoint, environment variable or credential. Missing, invalid, changed and unrecognized providers have no identity. The dsh preview displays an abbreviated digest and ignores absent or malformed fields. These identifiers are not a health check, remote revision, quality admission or authorization to send data.

#207 validation used synthetic service configurations: three focused service tests passed on source and the non-editable 0.1.71 wheel, including scoped access, provider drift, remote redaction and restart; the final remote/local identity assertions passed again on the wheel. The preview.60 client component test passed on source and the packed client, including drift and malformed-field fallback; client and service package files matched source. Client syntax/build and strict mypy on the changed service module passed. Running strict mypy on the entire existing service test file reported two `no-any-return` errors in unchanged helpers. No real model, cloud call, browser acceptance or daily-use admission is claimed.

In the recommended independent-service deployment, the service owns provider configuration. Python SDK (`ServiceClient.reflect`), TypeScript SDK (`MemoryClient.reflect`), authenticated service API, CLI and MCP share the existing `ReflectionEngine`, authorized original evidence, wire protocol, quote/span/version validation and temporary unverified result. This adds experimental infrastructure, not model or daily-use admission.

Local is the default. Existing schema1 profiles remain compatible; omission of `--reflection-config` disables reflection. Normal recall/context never calls a chat model. Choose one provider at trusted startup; changing it requires an explicit config change and service restart. There is no automatic fallback, difficulty router, request-level endpoint/key override or model-selected provider. “Local first” means the default is local, not an automatic local-to-cloud cascade.

Core 0.1.64 binds the service provider to the parsed configuration used at
startup. A changed, deleted or unreadable reflection config rejects new service
reflection calls with `reflection_config_changed`; it is also checked immediately
before each model turn. The trusted management diagnostic reports that a restart
is required without exposing the configuration. Restoring the original parsed
configuration resumes it; to apply a different one, restart the service. This
is not an atomic file-watcher or a revocation guarantee: a change can race the
last check, and an already dispatched request or data cannot be recalled.
Standalone/direct hosts remain responsible for their own provider lifecycle.

#197 validation used synthetic data and a loopback fixture. The initial
44-test service/extraction/reflection run had one failure because its synthetic
blocking worker mocked the former loader and supplied a nonexistent config;
the other 43 passed. After correcting that fixture, its disconnect/cancellation
regression and the new source and installed-wheel provider-drift checks passed.
The remote-dispatch guard was also checked against a loopback server with zero
requests. Source and installed dsh component checks, builds and strict mypy
passed. This does not establish browser acceptance or model/daily admission.

## Configuration outside the checkout

Schema2 local form wraps the old profile:

```json
{"schema_version":2,"provider":"local","local":{"profile":"qwen3-0.6b-experimental-reflection-v1","node":"C:/local/node.exe","transformers_dir":"C:/local/node_modules/@huggingface/transformers","model_dir":"C:/local/models/qwen3-0.6b"},"policy":{}}
```

Remote form contains an environment variable name, never a literal key:

```json
{
  "schema_version": 2,
  "provider": "openai-compatible",
  "remote": {
    "base_url": "https://your-provider.example/v1",
    "model": "your-explicit-model-id",
    "api_key_env": "EVERNOOK_REFLECTION_API_KEY",
    "allowed_clients": ["owner", "dsh"],
    "allow_loopback_http": false,
    "output_limit": "max_completion_tokens",
    "input_tokens": 4096,
    "output_tokens": 512,
    "pricing": {
      "input_micro_usd_per_million": 1000000,
      "output_micro_usd_per_million": 2000000,
      "request_micro_usd": 0
    },
    "max_cost_micro_usd": 30000,
    "daily_cost_micro_usd": 100000
  },
  "policy": {}
}
```

These are synthetic prices, **not a vendor quote**. One USD is1,000,000 micro-USD. Set rates from the selected vendor's contract. `max_cost_micro_usd` caps one query's local reservations, not monthly account spending. Supply the secret to the trusted provider host environment via the OS or an existing secret manager. In centralized deployment this is the service; SDK, browser and MCP request/response fields contain no key. This version does not persist/manage keys or collect them in the dashboard.

Core 0.1.65 adds optional `daily_cost_micro_usd` (positive integer, at most
1,000,000,000). After adding it, restart the service and run the trusted
`reflect-configure` command below to create its budget table. Without that
table, calls fail closed. The limit covers all opted-in remote calls on the
same ledger in a UTC calendar day, across clients, providers and service
restarts. Each turn atomically reserves its entire configured token/request
ceiling before dispatch; failure, cancellation, crash and unused tokens are
not refunded. A successful reservation adds its UTC day, cumulative reserved
amount and limit to the returned usage. A system date earlier than the last
reservation is rejected until the date catches up. The existing per-query
limit, authorization and egress checks continue to apply.

This is a conservative **local estimate**, not an invoice or account-wide
spending ceiling. Accurate rates are the owner's responsibility; free or
incorrectly priced requests, undisclosed provider charges, other ledgers,
other applications and calls made while the daily limit is disabled are not
covered. Restoring an older backup can roll back the local counter. A clock
jump into the future can also exhaust or reset a window incorrectly. Use the
provider's account-level quotas for an external spending ceiling; enabling or
changing this experimental option does not admit a model for daily use.

#198 validation: 44 source reflection/provider/service tests passed. Separate
source and non-editable wheel tests covered concurrent shared reservations,
missing setup, failed usage reporting, UTC rollover/clock rollback, and a real
isolated service killed after a loopback request arrived; its restarted process
retained the reservation. The installed-wheel set also rechecked protected
source exclusion. Strict mypy passed. No real provider billing, browser UI,
cross-device budget synchronization or production admission was tested.

Core 0.1.66 / dsh preview.56 exposes `reflection_daily_budget` through the
trusted `manage` status read. Its finite state is `disabled`, `setup_required`,
`ready`, `exhausted`, `clock_rollback`, `restart_required` or `unavailable`.
For a valid current day it includes UTC day, limit, reserved and remaining
micro-USD; it does not return a provider URL, key, query or source text. This
requires the same recall, maintain and accept scopes as management. The page
shows this as an advisory snapshot; concurrent reservations may change it
immediately. An old service without the field displays unknown. The status
read does not initialize, reset or reserve any budget.

#199 validation: the initial 22-test source run had 21 passes and one fixture
error because a test placed a client access file outside the required runtime
directory. The corrected permission/date/status cases passed in a three-test
source run and a four-test installed-wheel run. Source and installed page
components covered each status and old-service fallback; build and strict
mypy passed. A separate source and installed test checked consistent status
snapshots during concurrent reservations. No actual browser or daily model
admission is claimed.

The compatible standalone CLI (`--reflection-config` on a non-service-mode library) and trusted direct Python engine may themselves be provider hosts. Such a host explicitly owns the profile/environment key and still enforces client allowlist, call consent and the same evidence/budgets. It is distinct from a keyless service client. An active service-mode library still routes CLI through its service and cannot be bypassed with a caller-supplied profile path. Standalone/direct-host usage is not a new service-only guarantee.

`allowed_clients` contains paired client IDs, not entry/session names. A caller must also send `allow_remote=true` each time. MCP additionally requires trusted launcher `--allow-remote-reflection`; a model tool argument cannot grant that permission. TypeScript `MemoryConfig.allowRemoteReflection=true` grants launcher permission. dsh's trusted `reflection=true` plus `reflectionAllowRemote=true` (both default false) forwards consent as a constant, not a model-controlled parameter. Keys/endpoints are absent from dsh options.

```powershell
python -m evernook.service --config C:/outside/runtime/runtime.json --reflection-config C:/outside/remote-reflection.json
'{}' | python -m evernook.cli reflect-configure --config C:/outside/runtime/runtime.json --access C:/outside/runtime/mcp-access-owner.json --require-service
'{"query":"合成：旧相册里的经历是什么？","allow_remote":true}' | python -m evernook.cli reflect --config C:/outside/runtime/runtime.json --access C:/outside/runtime/mcp-access-owner.json --require-service
```

The generic authenticated service API accepts `cli/reflect` or `mcp/evernook_v1_reflect` with that same input. SDK calls:

```python
from evernook.service import ServiceClient, client_access
from pathlib import Path
config = Path("C:/outside/runtime/runtime.json")
access = client_access(config, config.parent / "mcp-access-owner.json")
result = ServiceClient(config).reflect(access, "合成：旧相册里的经历是什么？", allow_remote=True)
```

```typescript
const client = new MemoryClient({ python, config, access, entry: "dsh",
  serviceRequired: true, allowRemoteReflection: true });
await client.connect();
try { await client.reflect({ query: "合成：旧相册里的经历是什么？", allow_remote: true }); }
finally { await client.close(); }
```

## Egress and authority

Before dispatch the engine rechecks caller authorization and current input receipt. Private/protected/unavailable input receipts block external processing; credential-signalling questions are routed before embedding or providers. CLI questions without receipts require explicit consent; unmarked natural-language secrets cannot be inferred reliably, so callers must preserve the existing private-input boundary.

Only bounded current-question search/read/navigation/allowed-view evidence is eligible. Original spans are reread with active state, original representation, role, sensitivity, protected-source and version checks immediately before dispatch. Private/vault contents, revoked sources, full user profiles/persona, unrelated full history and generated reflections are not packet inputs. Keys are checked in original payload strings and decoded output/action strings, including nested JSON. The outgoing packet remains the minimal question/evidence IDs/quotes/roles/statement-time form and existing synthetic demonstrations.

The API receives no executable tools. Remote inference may only return a final answer/abstention/clarification/refusal over the host-selected excerpts. Its packet advertises these four allowed actions; a remote search/read/navigation/view is rejected **before** any further local exploration or HTTP request. Thus remote output cannot enlarge the outbound evidence set or choose a new topic. Local providers retain the existing bounded read-only exploration capability. Both use the same evidence, final output contract and validators; provider capabilities may be narrower. Additional cloud-directed exploration would require a separate explicit authorization policy, not merely allow_remote.

Write/persona/permission/vault/external actions and forged quotes are rejected. Valid quotes do not prove entailment. Existing finite guards remain separately labelled and earn no raw semantic credit. Results remain temporary unverified inferences, never automatic fact/profile/observation writes.

Source/control/authorization changes before handoff clear claims, proof and assessment as before. Dispatch is authorized at a local snapshot: already-sent data cannot be recalled after a later withdrawal. `store:false` requests non-storage under the API contract, not a guarantee of every vendor's retention/training policy. Same-account malicious code and an untrustworthy provider are outside local isolation guarantees.

## Limits and failures

HTTPS is required; URL userinfo/query/fragment are rejected. Explicit literal-loopback HTTP is the sole testing/local-server exception, not arbitrary LAN hosts. Redirects, ambient proxies, HTTP retries, streaming and fallback are disabled. The stdlib worker runs as a child with `-I -S`; key passes through stdin into Authorization, never argv/logs/errors/memory. Response bytes, duplicate JSON keys, usage, finish reason, action and quotes are checked. Deep malformed JSON safely degrades. Cancellation/deadline kills and reaps the owned worker, but cannot guarantee cancellation of provider-side work.

Remote per-turn caps default4096 input/512 output, configurable up to16384/4096 and still bounded by `ReflectionBudget` aggregate quotas and wall clock. Local caps stay4096/512. Increasing remote caps changes processor/policy identity and inherits no frozen/daily admission. The portable input preflight uses UTF-8 bytes plus framing allowance, **not an exact arbitrary-vendor tokenizer**. Actual usage is independently checked; missing/over-cap/malformed usage fails closed and unknown cost is never presumed free.

`max_completion_tokens` includes visible and reasoning tokens under the [documented Chat Completions contract](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create). A compatible API may require explicit `max_tokens`; verify that its completion usage includes charged reasoning. The adapter does not guess vendor semantics or silently drop unsupported safety/limit fields. Model-specific request extensions or non-compatible protocols need another explicit adapter.

Before each call, reserve the whole per-turn input/output cap at configured rates, rounding both components upward separately and adding request fees. Exceeding `max_cost_micro_usd` prevents dispatch. Failures/timeouts do not refund reservations. `usage.cost_reserved_micro_usd` and `usage.cost_reported_micro_usd` are distinct; the latter is calculated from returned token usage at configured prices, not a verified invoice. Unknown usage is marked incomplete. Vendor-injected tokens, wrong prices, dishonest usage or undisclosed charges cannot be hard-bounded by a generic client; external account quotas are needed for an external billing ceiling.

No real online model/key is selected or called by installation/tests. Fake HTTP fixtures validate transport/safety only. #120 and daily gates remain open until the actual model, configured limits and end-to-end behavior pass frozen evaluation and real-provider validation.
