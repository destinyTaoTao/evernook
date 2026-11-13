# dsh memory console: installed dark-theme dialog check

On 2026-11-14, an isolated dsh 0.2.1-rc.5 profile and an isolated EverNook
Core installation were used with synthetic records only. The preview.26 npm
package displayed the confirmed-profile/source network; opening a source and
confirming suppression removed that synthetic source from ordinary recall and
refreshed the displayed network. Its native source and confirmation dialogs
were almost white on white in the host's dark theme.

Preview.27 pairs the host's foreground and platform background theme variables
for dialogs. In a fresh isolated dsh profile with the installed preview.27
package, the source dialog and suppression preview were visibly readable in
the dark theme. The second suppression preview was cancelled. This check did
not use actual private memory or invoke a model.

The package build, JavaScript syntax, network test, host lifecycle/web tests,
and harness TypeScript check passed. This is not a full accessibility audit,
light-theme verification, or acceptance of the remaining cross-layer network,
filtering, configuration diagnostics, and daily-use requirements in #114.
