// UI regression with mocked count metadata only: never uploads/processes 100K data.
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';

const base = process.env.TRACEX_E2E_BASE ?? 'http://localhost:5173';
const output = process.env.TRACEX_E2E_OUTPUT ?? path.resolve('../var/browser-runs', `review-counts-${Date.now()}`);
await mkdir(path.dirname(output), {recursive: true});
await mkdir(output, {recursive: false});
const report = {scope: 'MOCKED COUNT METADATA ONLY; not a 100K upload, grouping benchmark or quality evaluation', checks: []};
const check = (name, ok) => { report.checks.push({name, ok}); assert.ok(ok, name); console.log(`PASS ${name}`); };
const browser = await chromium.launch({channel: 'chrome', headless: true});
const context = await browser.newContext();
await context.addInitScript(() => localStorage.setItem('tracex.auth', JSON.stringify({actor: 'mock-counter-reviewer', token: 'mock-not-a-real-session'})));
let unresolved = 180;
let incomplete = false;
let unavailable = false;
const streams = [];
const cases = ['counter-one', 'counter-two'].map(case_id => ({case_id, name: 'Same display name', role: 'case_lead', synthetic: true, created_at: '2026-01-01T00:00:00Z'}));
const counts = (caseId, capacity, offset) => {
  const unresolved_groups = incomplete ? 0 : caseId === 'counter-one' ? unresolved : 7;
  const queued_groups = Math.min(capacity, unresolved_groups);
  const group = {group_id: `${caseId}-group`, case_id: caseId, family: 'fixture/v1', status: 'open', member_count: 3,
    window_start: '2026-01-01T00:00:00Z', window_end: '2026-01-01T01:00:00Z', focal_ref: 'tx:fixture'};
  return {policy: 'group-family-round-robin-v1', capacity, underlying_findings: 23000, investigation_groups: incomplete ? 0 : caseId === 'counter-one' ? 180 : 7,
    unresolved_groups, queued_groups, backlog_groups: Math.max(0, unresolved_groups-capacity), filtered_total: queued_groups,
    items: queued_groups && !offset ? [group] : [], scope: 'queue', grouping_coverage: {
      state: incomplete ? 'incomplete' : 'complete', grouped_findings: incomplete ? 0 : 23000, ungrouped_findings: incomplete ? 23000 : 0,
      reason: incomplete ? 'Older import requires authorized analysis retry.' : null}};
};
await context.route('**/v1/**', async route => {
  const url = new URL(route.request().url());
  const endpoint = url.pathname.slice(3);
  const json = body => route.fulfill({json: body});
  if (endpoint.endsWith('/events')) { streams.push(route); return; }
  if (endpoint === '/cases') return json({cases});
  const caseId = endpoint.split('/')[2];
  if (endpoint.endsWith('/investigation-queue')) {
    if (unavailable) return route.fulfill({status: 404, json: {detail: 'Not Found'}});
    return json(counts(caseId, Number(url.searchParams.get('capacity') ?? 100), Number(url.searchParams.get('offset') ?? 0)));
  }
  if (endpoint.endsWith('/review-queue')) return json({capacity: 100, items: [], eligible_scored_transactions: 100000,
    threshold_flagged_transactions: 0, queued_transactions: 0, additional_flagged_transactions: 0, filtered_total: 0});
  if (endpoint.endsWith('/findings')) return json({findings: [], total: 23000, open_total: 23000, methods: [], ml_enabled: false,
    review_policy: {version: 'fixture'}, limit: 200, offset: 0});
  if (/^\/cases\/[^/]+$/.test(endpoint)) return json({...cases.find(c => c.case_id === caseId), members: []});
  throw new Error(`Unexpected API call in counter-only fixture: ${endpoint}`);
});
const page = await context.newPage();
try {
  await page.goto(`${base}/cases/counter-one/dashboard`);
  // Use shared primitive contents rather than a sample-list length.
  await page.getByText('100', {exact: true}).first().waitFor();
  check('dashboard main workload is 100 queued groups, not 23,000 open findings', (await page.locator('.stat-grid').textContent()).includes('In your review queue') && !(await page.locator('.stat-grid').textContent()).includes('Open findings'));
  check('dashboard preserves actual observation count and the 80-group backlog', (await page.locator('.stat-grid').textContent()).includes('23,000') && (await page.locator('.stat-grid').textContent()).includes('80 additional unresolved'));
  unresolved = 179;
  for (const route of streams.splice(0)) await route.fulfill({contentType: 'text/event-stream', body: 'id: 1\nevent: investigation_group.reviewed\ndata: {}\n\n'});
  await page.getByText(/79 additional unresolved groups/).waitFor();
  check('review event refreshes durable queue/backlog counts without a page reload', true);
  await page.goto(`${base}/overview`);
  await page.getByText(/107 groups in your review queues/).waitFor();
  const rows = page.locator('.data-table tbody tr');
  await rows.nth(1).getByText('7', {exact: true}).waitFor();
  check('overview aggregates queued groups, not unresolved groups or preview length', (await page.locator('.stat-grid').innerText()).includes('107'));
  check('same-named cases retain independent backend totals', (await rows.nth(0).innerText()).includes('100') && (await rows.nth(1).innerText()).includes('7'));
  await page.goto(`${base}/cases/counter-one/findings`);
  await page.getByTestId('review-workload').filter({hasText: '100 groups'}).waitFor();
  check('findings feed prioritizes the grouped queue and labels raw observations separately', await page.getByTestId('underlying-observation-counts').count() === 1 && await page.locator('.page-header .ff-counters').count() === 0);
  await page.getByLabel('Unresolved group capacity').fill('1');
  await page.getByTestId('review-workload').filter({hasText: '1 groups in your current review queue'}).waitFor();
  await page.getByLabel('Unresolved group capacity').fill('0');
  await page.getByTestId('review-workload').filter({hasText: '0 groups'}).waitFor();
  check('zero/one capacity keeps the actual unresolved backlog accessible', (await page.getByTestId('group-counts').innerText()).includes('179 additional unresolved groups'));
  incomplete = true;
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await page.getByRole('alert').filter({hasText: /Grouping incomplete: 23,000/}).waitFor();
  check('unmaterialized older imports do not silently appear fully reviewed', true);
  unavailable = true;
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await page.getByRole('alert').filter({hasText: /Group queue unavailable/}).waitFor();
  check('stale backend route failure is explicit, with no raw-count or fake-zero fallback', await page.getByTestId('review-workload').count() === 0);
  report.status = 'pass';
} catch (error) { report.status = 'fail'; report.error = String(error); process.exitCode = 1; }
finally {
  await writeFile(path.join(output, 'result.json'), JSON.stringify(report, null, 2));
  await browser.close();
}
