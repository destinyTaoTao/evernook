import assert from 'node:assert/strict';
import test from 'node:test';
import { completeObject } from '../../src/evernook/reflection_json.mjs';

test('generation stops only at a whole JSON object without repairing partial text', () => {
  for (const text of ['', '{', '{"action":"abstain","support":{}', '{"q":"escaped \\"}', '[]', 'null', 'true',
    '{"a":1} trailing', '{"a":1}{"b":2}', '```json\n{}\n```']) assert.equal(completeObject(text), false, text);
  for (const text of ['{}', ' {"action":"abstain","support":{}}\n', '{"q":"braces }{ and \\" quotes"}']) {
    assert.equal(completeObject(text), true, text);
  }
  // Python's unique-key parser remains responsible for duplicate keys and all action validation.
  assert.equal(completeObject('{"a":1,"a":2}'), true);
});
