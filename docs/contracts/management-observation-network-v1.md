# Trusted management observation nodes

`manage` with `action: read` and `view: network_observations` returns pages of
published or conflicted observation slots under the existing recall, maintain and
accept authorization. Each visible item exposes its conclusion, state, revision,
inference flag and separate support and counterevidence message IDs. Source
text and span contents are not copied into the response; source opening remains
a separate authorized read.

Each read uses one database snapshot, validates every source and confirmation,
and omits unavailable, withdrawn, candidate, future and expired observations.
Since validation happens after bounded slot pagination, a page can be empty
while a later page exists. Query and category match the conclusion and category;
entry, session and half-open occurrence time must all match one validated
support or counterevidence source. This endpoint does not admit daily use.
