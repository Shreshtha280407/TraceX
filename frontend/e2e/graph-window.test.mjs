import assert from 'node:assert/strict';
import test from 'node:test';
import { mergeGraphPage, graphWindow, MAX_LOADED_NODES, MAX_LOADED_EDGES } from '../src/lib/graphWindow.ts';

const page = (start, count, snapshot = 'graph') => ({graph_snapshot_id: snapshot,
  nodes: Array.from({length: count}, (_, i) => ({id: `n${start+i}`})),
  edges: Array.from({length: count-1}, (_, i) => ({id: `e${start+i}`, from: `n${start+i}`, to: `n${start+i+1}`}))});
test('continuation is consumed by bounded visual windows, not just node lists', () => {
  const merged = mergeGraphPage(page(0, 100), page(90, 100));
  assert.equal(merged.nodes.length, 190);
  assert.equal(graphWindow(merged, 1).nodes[0].id, 'n100');
  assert.equal(graphWindow(merged, 1).edges.length, 89);
  assert.ok(graphWindow(merged, 1).hiddenEdges > 0);
  const bounded = mergeGraphPage(merged, page(0, 2000));
  assert.equal(bounded.nodes.length, MAX_LOADED_NODES);
  assert.equal(bounded.edges.length, MAX_LOADED_EDGES);
  assert.throws(() => mergeGraphPage(merged, page(0, 3, 'changed')), /Snapshot changed/);
});
