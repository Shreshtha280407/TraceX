import type { InvestigationQueue } from "./api";

/** Never derive the human workload from raw alerts or a paginated preview. */
export function reviewCounts(queue: InvestigationQueue) {
  return { findings: queue.underlying_findings, groups: queue.investigation_groups,
    unresolved: queue.unresolved_groups, queued: queue.queued_groups, backlog: queue.backlog_groups,
    ungrouped: queue.grouping_coverage?.ungrouped_findings ?? null };
}

export function aggregateReviewCounts(queues: Array<InvestigationQueue | null>) {
  if (queues.some(queue => queue === null)) return null;
  return queues.reduce((total, queue) => {
    const counts = reviewCounts(queue!);
    return { findings: total.findings + counts.findings, groups: total.groups + counts.groups,
      unresolved: total.unresolved + counts.unresolved, queued: total.queued + counts.queued,
      backlog: total.backlog + counts.backlog };
  }, {findings: 0, groups: 0, unresolved: 0, queued: 0, backlog: 0});
}

export const REVIEW_COUNT_EVENTS = new Set(["import.completed", "import.failed", "investigation_group.reviewed"]);
