// Real local synthetic case only; no mocked stages/counts or large data.
import assert from 'node:assert/strict';
import {writeFile} from 'node:fs/promises';
import {chromium} from 'playwright';
const report = {scope: 'Small authenticated real CSV queue/navigation check; not model quality', checks: []};
const check = (name, ok) => { report.checks.push({name, ok: Boolean(ok)}); assert.ok(ok, name); console.log('PASS '+name); };
const browser = await chromium.launch({channel: 'chrome', headless: true});
try {
  const context = await browser.newContext();
  const caseId = process.env.TRACEX_SPEED_CASE;
  await context.addInitScript(({auth, caseId, jobId}) => {
    localStorage.setItem('tracex.auth', JSON.stringify(auth));
    localStorage.setItem('tracex.jobRegistry', JSON.stringify({[caseId]: [jobId]}));
  }, {auth: JSON.parse(process.env.TRACEX_SPEED_AUTH), caseId, jobId: process.env.TRACEX_SPEED_JOB});
  const page = await context.newPage();
  await page.goto(`${process.env.TRACEX_E2E_BASE}/cases/${caseId}/dashboard`);
  const tile = page.locator('.stat-tile').filter({has: page.locator('.label').getByText('In your review queue', {exact: true})});
  await tile.locator('.value').getByText(process.env.TRACEX_SPEED_QUEUE, {exact: true}).waitFor();
  check('dashboard shows actual backend queue count', true);
  await page.getByRole('link', {name: 'Review', exact: true}).first().click();
  await page.getByRole('heading', {name: 'Review proposition', exact: true}).waitFor();
  await page.getByRole('heading', {name: 'Member evidence', exact: true}).waitFor();
  check('real group proposition and member evidence opens', true);
  const groupId = new URL(page.url()).pathname.split('/').at(-1);
  const token = JSON.parse(process.env.TRACEX_SPEED_AUTH).token;
  const apiBase = process.env.TRACEX_SPEED_API_BASE.replace(/\/$/, '');
  async function api(endpoint, options = {}) {
    const response = await context.request.fetch(`${apiBase}/v1${endpoint}`, {
      ...options, headers: {Authorization: `Bearer ${token}`}, timeout: 15000});
    assert.ok(response.ok(), `${response.status()} ${endpoint}`);
    return response.json();
  }
  const before = await api(`/cases/${caseId}/investigation-queue`);
  const member = (await api(`/investigation-groups/${groupId}/members`)).items[0].finding;
  await page.getByLabel('Recorded reason').fill('Small CSV functional test: bounded group proposition only');
  await page.getByRole('button', {name: 'Record group decision', exact: true}).click();
  await page.getByText('Decision 1: triaged — Small CSV functional test: bounded group proposition only', {exact: true}).waitFor();
  const after = await api(`/cases/${caseId}/investigation-queue`);
  const detail = await api(`/investigation-groups/${groupId}`);
  const sameMember = (await api(`/investigation-groups/${groupId}/members`)).items[0].finding;
  check('decision persists and frees one unresolved task without changing individual verdict',
    detail.status === 'triaged' && after.unresolved_groups === before.unresolved_groups - 1 && sameMember.status === member.status);
  await page.reload();
  await page.getByText('Decision 1: triaged — Small CSV functional test: bounded group proposition only', {exact: true}).waitFor();
  check('persisted decision survives browser refresh', true);
  report.status = 'pass';
} catch (error) { report.status = 'fail'; report.error = String(error); process.exitCode = 1; }
finally {
  await writeFile(process.env.TRACEX_SPEED_BROWSER_REPORT, JSON.stringify(report, null, 2));
  await browser.close();
}
