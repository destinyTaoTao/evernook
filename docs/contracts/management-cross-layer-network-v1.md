# Trusted cross-layer evidence network

`manage` with `action: read` and `view: network_graph` reads bounded slots from
the profile, temporal fact, observation and topic registries in one authorized SQLite
snapshot. It requires recall, maintain and accept scopes. Nodes include only
the current validated content, revision and state. Source identifiers form
edges labelled as evidence, support or counterevidence; source text is never
copied into the response and opening a source performs a separate current read.

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
