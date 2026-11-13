# Trusted cross-layer evidence network

`manage` with `action: read` and `view: network_graph` reads bounded slots from
the profile, temporal fact and observation registries in one authorized SQLite
snapshot. It requires recall, maintain and accept scopes. Nodes include only
the current validated content, revision and state. Source identifiers form
edges labelled as evidence, support or counterevidence; source text is never
copied into the response and opening a source performs a separate current read.

Each layer consumes up to `limit` slots at the shared offset. A non-null next
offset means at least one layer has more slots; pages may be empty after
validation and filtering. Text and category match nodes, while entry, session
and half-open occurrence time must match the same valid supporting or opposing
source. The display joins shared source IDs among nodes on this page only; it
does not claim a complete graph across pages. A source may connect multiple
types without implying the linked conclusions are equivalent.

Source and approval checks are performed at read time. A second client can
change controls after the response; existing visible node labels are refreshed
on focus or manual refresh, not instantly invalidated. Topic snapshots and
reflection paths are not yet included, and this endpoint does not change daily
admission.
