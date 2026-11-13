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
while a later page exists. Unsupported query, category and session filters fail
explicitly. The UI, additional layers and daily-use admission are not part of
this endpoint.
