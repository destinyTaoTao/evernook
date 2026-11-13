"""One synthetic-only evidence-first decoding control; frozen messages stay identical."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from probes.admission import local_reflection as native
from probes.admission.structured_reflection import schema


def ordered_schema(payload: dict[str, Any], wire: str) -> dict[str, Any]:
    if wire != 'flat':
        raise ValueError('invalid_candidate_order')
    result = schema(payload, wire)
    for alternative in result['oneOf']:
        properties = alternative['properties']
        if 'support' in properties:
            alternative['properties'] = {'support': properties['support'],
                **{key: value for key, value in properties.items() if key != 'support'}}
    # Required fields, possible decisions/values and citation constraints are unchanged.
    return result


def collect(assets: Path, output: Path, node: Path, jinja: Path) -> dict[str, Any]:
    driver = Path(__file__).resolve()
    identity = native.digest(driver)
    with patch.object(native, 'schema', ordered_schema):
        result = native.collect(assets, 'qwen35-2b-q4', output, node, jinja,
            mode='greedy', seed=12009, wire='flat', constrained=True, compact_memory=False)
    result['identity'][driver.name] = identity
    result['collector_unchanged'] = result['collector_unchanged'] and native.digest(driver) == identity
    result['decoding_order'] = 'support,action,value; only native schema property order, not messages'
    result['scope'] += ' Evidence-first experiment: schema has identical canonical JSON semantics; no automatic citation completion or answer selection.'
    (output / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('assets', 'output', 'node', 'jinja'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    collect(args.assets, args.output, args.node, args.jinja)
