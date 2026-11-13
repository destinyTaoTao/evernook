# Trusted management fact nodes

`manage` with `action: read` and `view: network_facts` returns a bounded page
of current temporal fact slots. It requires the existing recall, maintain and
accept scopes. Each visible item contains its fact ID, subject, predicate,
revision, state, text and source message IDs; opening a source still requires a
separate authorized management read. Evidence spans, confirmation identifiers
and evidence text are not duplicated in this response.

The current fact is selected using the fact registry's temporal snapshot before
its sources and confirmation are checked. An unavailable successor therefore
does not resurrect a previous value. A disputed slot retains its disputed state;
the returned current assertion must not be displayed as an uncontested fact.
The page is over slots, so withheld entries can produce an empty page with a
non-null next offset. Query and category are matched against the selected fact;
entry, session and half-open occurrence time must all match the same validated
source. This endpoint is not a complete network UI;
other layers, filters, live invalidation and daily-use admission remain open.
