# EverNook native harness adapters

The dsh adapter targets `0.2.1-rc.3` with Cordis `4.0.5`, Node `24.11.x` and the same authenticated Python ledger. It does not activate or modify a desktop profile. See the [contract](../../docs/contracts/dsh-native-v1.md) for configuration, source identity, bounded attachments, buffered publication and remaining acceptance work.

```powershell
npm ci --ignore-scripts
npm run typecheck
npm run build
$env:EVERNOOK_TEST_PYTHON = '<absolute external dev-venv python.exe>'
$env:EVERNOOK_TEST_OUTPUT = '<absolute external synthetic-only test directory>'
npm test
```

Tests run real local harness services and a deterministic synthetic provider. No model API or user profile is needed. The Python environment must have this checkout installed with `.[mcp]`. Runtime data and credentials must remain outside the checkout. The package is private; no npm publication or desktop installation is claimed.
