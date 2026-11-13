# Cross-layer network: isolated installed check

An isolated dsh 0.2.1-rc.5 profile, Core 0.1.27 wheel and preview.32 tarball
used a synthetic ledger. The installed Core management module and browser
client matched the checked-out source bytes; the host verifier checked 61
dependency edges. A real CLI call through the separate service returned three
nodes sharing one source. In the browser the same source connected a confirmed
profile, current fact and conflicted inferred observation; a second source was
labelled counterevidence. Opening the shared source returned its synthetic
original. No chat or memory was sent to a model and no inference was run.

The source suppression preview listed the profile and fact but omitted the
dependent observation. The preview was cancelled; #133 tracks this defect.
The graph is bounded to the current slots and does not promise instantaneous
cross-client invalidation, full database coverage, topics or reflection paths.
Light/mobile, large-scale and daily-use acceptance were not part of this check.

The dedicated dsh profile inherited a configured model provider; the dsh host
fetched its provider model list at startup. This was not an EverNook reflection
request and did not contain the synthetic memory. Runtime material and logs
remain outside the checkout.
