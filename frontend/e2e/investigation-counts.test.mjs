import assert from 'node:assert/strict';
import test from 'node:test';
import { aggregateReviewCounts, pendingReviewQueueCount, reviewCounts, REVIEW_COUNT_EVENTS } from '../src/lib/investigationCounts.ts';

const summary = (overrides = {}) => ({ underlying_findings: 23000, investigation_groups: 180,
  unresolved_groups: 180, queued_groups: 100, backlog_groups: 80, items: [{ group_id: 'preview-one' }],
  grouping_coverage: { state: 'complete', grouped_findings: 23000, ungrouped_findings: 0 }, ...overrides });

test('23,000 observations do not become 23,000 queued reviews, and preview length is not a counter', () => {
  assert.deepEqual(reviewCounts(summary()), {findings: 23000, groups: 180, unresolved: 180, queued: 100, backlog: 80, ungrouped: 0});
  for (const capacity of [0, 1, 100]) {
    const counts = reviewCounts(summary({queued_groups: capacity, backlog_groups: 180-capacity, items: []}));
    assert.equal(counts.queued, capacity);
    assert.equal(counts.queued + counts.backlog, counts.unresolved);
  }
});

test('overview aggregates backend queue/backlog totals and never silently treats unavailable data as zero', () => {
  assert.deepEqual(aggregateReviewCounts([summary(), summary({queued_groups: 5, backlog_groups: 0, unresolved_groups: 5})]),
    { findings: 46000, groups: 360, unresolved: 185, queued: 105, backlog: 80 });
  assert.equal(aggregateReviewCounts([summary(), null]), null);
  assert.equal(reviewCounts(summary({grouping_coverage: undefined})).ungrouped, null);
  assert.equal(reviewCounts(summary({grouping_coverage: {ungrouped_findings: 23000}, queued_groups: 0})).ungrouped, 23000);
});

test('terminal import and independent group decisions refresh the workload; raw finding review is not group closure', () => {
  assert.ok(REVIEW_COUNT_EVENTS.has('import.completed'));
  assert.ok(REVIEW_COUNT_EVENTS.has('import.failed'));
  assert.ok(REVIEW_COUNT_EVENTS.has('investigation_group.reviewed'));
  assert.ok(REVIEW_COUNT_EVENTS.has('analysis.grouping_queued'));
  assert.ok(!REVIEW_COUNT_EVENTS.has('finding.reviewed'));
  assert.ok(!REVIEW_COUNT_EVENTS.has('batch.committed'));
});

test('a legacy ungrouped case is not zero pending reviews, and real queues stay bounded', () => {
  assert.equal(pendingReviewQueueCount(null), null);
  assert.equal(pendingReviewQueueCount(summary({queued_groups: 0, capacity: 100,
    grouping_coverage: {state: 'incomplete', ungrouped_findings: 23000}})), null);
  assert.equal(pendingReviewQueueCount(summary({queued_groups: 0, capacity: 100,
    grouping_coverage: {state: 'complete', ungrouped_findings: 0, active_jobs: 1}})), null);
  for (const queued_groups of [0, 1, 42, 100]) {
    assert.equal(pendingReviewQueueCount(summary({queued_groups, capacity: 100})), queued_groups);
  }
  assert.equal(pendingReviewQueueCount(summary({queued_groups: 0, capacity: 0,
    grouping_coverage: {state: 'incomplete', ungrouped_findings: 23000}})), 0);
});
