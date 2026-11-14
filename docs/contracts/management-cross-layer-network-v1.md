# Trusted cross-layer evidence network

`manage` with `action: read` and `view: network_graph` reads bounded slots from
the profile, temporal fact, observation and topic registries in one authorized SQLite
snapshot. It requires recall, maintain and accept scopes. Nodes include only
the current validated content, revision and state. Source identifiers form
edges labelled as evidence, support or counterevidence; source text is never
copied into the response and opening a source performs a separate current read.

Core 0.1.62 / dsh preview.53 adds the source role (`user`, `assistant` or `tool`)
to each graph edge. Roles are read in bounded batches from the same authorized
snapshot after evidence validation; missing or inconsistent sources fail closed.
The console groups by source and labels the role, using unknown when an older
service omits it or supplied roles disagree. A role does not prove truth,
speaker identity or user confirmation; in particular a tool transcription is
not promoted to a user statement. The graph still omits source text.

#195 源码管理／主题回归 51 项、非 editable wheel 定向 4 项、源码真实已激活服务
定向 1 项，以及源码／安装版页面组件各 2 项和 strict mypy 通过。合成测试覆盖
用户、助手、工具、反证、撤回和旧服务缺字段；已激活服务测试确认图响应不复制
原文。尚未对本次页面进行实际浏览器或大规模性能验收，日用门禁不变。

Only active topics with a currently valid or conflicted snapshot contribute;
topic nodes expose the definition title, snapshot revision and evidence edges,
not the snapshot body. Paused, stale and expired topics are withheld. Topic
validity uses the service-configured topic processor and embedding identity.
Topic nodes have no category, so a category filter excludes them.

Each layer consumes up to `limit` slots at the shared offset. A non-null next
offset means at least one layer has more slots; pages may be empty after
validation and filtering. Text and category match nodes, while entry, session
and half-open occurrence time must match the same valid supporting or opposing
source. The display joins shared source IDs among nodes on this page only; it
does not claim a complete graph across pages. A source may connect multiple
types without implying the linked conclusions are equivalent.

dsh preview.48 exposes an exact category identifier input for the cross-layer,
profile, fact and observation network views, including custom categories. The
filter is applied on search and reset when changing layer or tab; empty input
means all categories. The page explains that filtering by category excludes
topics because topic nodes have no category. Time, entry, session and sparse
slot pagination keep the semantics above. The input uses the same authorized
versioned read and fail-closed lease as the rest of the page.

#187 synthetic component tests passed for source and installed preview.48,
including custom identifiers, clearing the filter and changing layers; two
backend tests covered category selection and topic exclusion. TypeScript build,
client syntax and package installation passed. No actual browser UI acceptance
or daily admission is claimed for this change.

Preview.49 (#188) preserves category identifiers exactly as entered,
including leading/trailing spaces, matching the existing backend's exact
comparison; only an empty string selects all categories. The preview.48 trim
could not retrieve valid space-containing custom categories. The synthetic
component regression failed before the fix; source and installed client tests
passed afterward, as did an installed Core backend test with distinct spaced
and unspaced identifiers. No actual browser UI acceptance is claimed.

Source and approval checks are performed at read time. In the dsh preview,
versioned page, source and history reads use authenticated fingerprint checks
and a 12-second fail-closed lease. A change triggers refresh; disconnect,
expiry or backgrounding clears visible data when the browser can execute the
handler. The checks are bounded polling, not push or instant revocation: an
already delivered value cannot be taken back, and a suspended event loop cannot
clear it until execution resumes. The graph is page-local, does not show
one-off reflection paths, and does not change daily admission.

#184 adds simulated clock and event-loop checks for a pause longer than the
lease: pending timers clear both the visible page and an open source when they
resume. A separate case advances wall time while monotonic time is stationary
and delivers an in-flight matching fingerprint before the delayed timeout; the
expired page stays hidden. The same component check passed against the installed
preview.47 client with the source-matching hash. This does not verify physical
sleep, screen lock, browser scheduling guarantees or multiple windows.
