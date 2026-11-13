# EverNook native harness adapters

The dsh adapter targets `0.2.1-rc.3` with Cordis `4.0.5`, Node `24.11.x` and the same authenticated Python ledger. It does not activate or modify a desktop profile. See the [contract](../../docs/contracts/dsh-native-v1.md) for configuration, source identity, bounded attachments, buffered publication and remaining acceptance work.

Explicit `contextConfig` and `memoryBudget` enable the [local target tokenizer mode](../../docs/contracts/host-token-budget-v1.md). This uses the shared core, checks the actual projected request and removes superseded memory sections. The trusted host supplies the provider's framing counter. Legacy character mode remains separately identified; it is not token-budget acceptance.

The opt-in pi SDK adapter targets `@earendil-works/pi-coding-agent` / `pi-ai` `1.4.0`. Its dedicated provider and explicitly wrapped tools save before publication; see the [pi contract](../../docs/contracts/pi-native-v1.md) for model selection, the required built-in tool mask, text-only support and acceptance limits. Both entries share the same authoritative ledger.

The explicit `./dsh-maintenance` export and optional pi `maintenance` configuration share reviewed pin, correction, suppression and purge proposals. They require separate maintenance credentials and trusted preview/result callbacks; only a later actual native user confirmation can apply a proposal. See the [maintenance contract](../../docs/contracts/host-maintenance-v1.md). Installing them does not change a desktop profile or grant the model maintenance scopes.

dsh can explicitly select `streamMode: 'durable_text'` to publish a single text block incrementally through the core chunk barrier. Tool/reasoning/multiple-block paths are rejected in this mode. `streams(sessionId, offset?)` discovers durable attempts after a restart; `readStream(source, cursor?, limit?)` pages only committed text for a trusted renderer. The renderer owns its display cursor; no provider resume or exactly-once display is implied. The default `buffered` mode remains a compatibility/validation path. See the [reliable dsh contract](../../docs/contracts/dsh-native-v1.md#可靠文本模式m58-14).

pi supports the same explicit `streamMode: 'durable_text'`, shared chunk barrier and recovery APIs, using the returned `evernook-durable-pi-v1` model. It cannot be combined with `tools` or `maintenance`; wrapped tool results remain supported only by the buffered profile. Native partial snapshots are rebuilt from committed text, and raw provider observations/usage metadata are excluded. The [pi contract](../../docs/contracts/pi-native-v1.md#可靠文本模式m58-15) defines the support matrix and first-fragment timing boundary.

```powershell
npm ci --ignore-scripts
npm run typecheck
npm run build
$env:EVERNOOK_TEST_PYTHON = '<absolute external dev-venv python.exe>'
$env:EVERNOOK_TEST_OUTPUT = '<absolute external synthetic-only test directory>'
npm test
```

Tests run real local harness services and a deterministic synthetic provider. No model API or user profile is needed. The Python environment must have this checkout installed with `.[mcp]`. Runtime data and credentials must remain outside the checkout. The package is private; no npm publication or desktop installation is claimed.

Reflection SDK: MemoryClient.reflect accepts the existing question/options plus explicit allow_remote. Remote requires trusted MemoryConfig.allowRemoteReflection at MCP startup and service allowlist; dsh uses reflectionAllowRemote (default false). Keys/endpoints belong only to the service. See [provider contract](../../docs/contracts/reflection-providers-v1.md). No automatic cloud fallback or daily model admission.
