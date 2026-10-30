# Synthetic local extraction integration

This runs the explicit CLI against the already prepared, pinned R0 Qwen candidate. It never downloads models and never enables automatic fact acceptance.

```powershell
<dev-python> verify.py --output E:/new-synthetic-extraction-run --node <absolute-node.exe> --transformers-dir <absolute-transformers-4.2.0-dir> --model-dir <absolute-R0-Qwen-model-dir>
```

The output directory must be new and outside the checkout; its parent must exist. Source text is a fixed synthetic Chinese preference. The report contains job outcome, verified candidates, elapsed time and original-preservation check. `ok=true` is a CLI operation receipt; inspect `run.state/error_code` for actual inference outcome. Quality admission remains false, and this probe does not disable the user's OS network.
