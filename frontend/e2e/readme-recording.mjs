// Real browser recordings. Every product response comes from the local API.
import { chromium } from "playwright";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

const base = process.env.TRACEX_RECORD_BASE;
const source = process.env.TRACEX_RECORD_SOURCE;
const output = process.env.TRACEX_RECORD_OUTPUT;
if (!base || !source || !output) throw new Error("Use scripts/capture_readme.py to start the isolated stack.");
const origin = new URL(base).origin;
if (!["127.0.0.1", "localhost"].includes(new URL(base).hostname)) throw new Error("Capture requires a local disposable stack.");
await mkdir(output, { recursive: true });
const awaitedBytes = await readFile(source);
const browser = await chromium.launch({ channel: "chrome", headless: true });
const report = { schema: "tracex-readme-recording-v1", synthetic: true, mocked_responses: false,
  viewport: { width: 1600, height: 1000 }, clips: [], checks: [], external_requests: [], page_errors: [] };
let token, actor, caseId, findings, group, job;
const hold = ms => new Promise(resolve => setTimeout(resolve, ms));
const check = (name, ok) => { report.checks.push({ name, passed: Boolean(ok) }); if (!ok) throw new Error(name); console.log(`PASS ${name}`); };
async function scene(name, fn, authenticated = true) {
  const context = await browser.newContext({ viewport: report.viewport, colorScheme: "dark",
    recordVideo: { dir: process.env.TRACEX_RECORD_WORK, size: report.viewport } });
  await context.route("**/*", async route => {
    const url = route.request().url();
    if (/^https?:/.test(url) && new URL(url).origin !== origin) {
      report.external_requests.push(url); await route.abort();
    } else await route.continue();
  });
  if (authenticated) await context.addInitScript(({ token, actor }) => {
    localStorage.setItem("tracex.auth", JSON.stringify({ token, actor }));
  }, { token, actor });
  const page = await context.newPage();
  page.on("pageerror", error => report.page_errors.push(error.message));
  const video = page.video();
  async function api(endpoint, options = {}) {
    const response = await context.request.fetch(`${base}/v1${endpoint}`, {
      ...options, headers: { Authorization: `Bearer ${token}`, ...(options.headers ?? {}) }, timeout: 120000,
    });
    if (!response.ok()) throw new Error(`${response.status()} ${endpoint}: ${(await response.text()).slice(0, 500)}`);
    return response.json();
  }
  async function click(locator) {
    await locator.scrollIntoViewIfNeeded();
    const bounds = await locator.boundingBox();
    if (bounds) await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2, { steps: 18 });
    await hold(250); await locator.click(); await hold(600);
  }
  async function shot(file) { await page.evaluate(() => document.fonts.ready); await hold(700); await page.screenshot({ path: path.join(output, file) }); }
  try {
    await fn({ page, api, click, shot });
    await hold(1200);
  } finally {
    await context.close();
    await video.saveAs(path.join(output, `${name}.webm`));
  }
  report.clips.push({ name, file: `${name}.webm`, playback: "continuous Playwright browser video; GIF is a downsampled preview" });
}
try {
  await scene("intake", async ({ page, api, click, shot }) => {
    await page.goto(base);
    await shot("landing.png"); await hold(1600);
    await click(page.locator(".landing-secondary"));
    await page.locator("#name").pressSequentially("Demo Investigator", { delay: 50 });
    await page.fill("#password", "synthetic-capture-only-password");
    await click(page.locator(".auth-submit"));
    await page.waitForURL("**/overview");
    ({ token, actor } = await page.evaluate(() => JSON.parse(localStorage.getItem("tracex.auth"))));
    check("Real browser signup and bearer authentication", Boolean(token));
    await click(page.getByRole("button", { name: "+ New Case" }));
    await page.getByLabel("Case name").pressSequentially("AMBER / Synthetic Bitcoin Investigation", { delay: 24 });
    await page.getByLabel("Synthetic/demo data (not real case evidence)").check();
    await page.getByLabel("Scoring procedure").selectOption("unsupervised");
    const creating = page.waitForResponse(r => r.url().endsWith("/v1/cases") && r.request().method() === "POST");
    await click(page.getByRole("button", { name: "Create case", exact: true }));
    const created = await (await creating).json(); caseId = created.case_id;
    check("UI creates a synthetic case with pinned unsupervised scoring", Boolean(caseId));
    await page.goto(`${base}/cases/${caseId}/ingestion`); await hold(1500);
    const uploading = page.waitForResponse(r => r.url().endsWith(`/cases/${caseId}/imports`) && r.request().method() === "POST");
    await page.locator("#evidence-file-input").setInputFiles(source);
    const uploaded = await uploading; check("UI upload accepted by the real API", uploaded.status() === 202);
    const jobId = (await uploaded.json()).job_id;
    const deadline = Date.now() + 180000;
    do {
      job = await api(`/jobs/${jobId}`);
      if (["completed", "failed"].includes(job.state)) break;
      await hold(700);
    } while (Date.now() < deadline);
    check("Real worker finishes the synthetic import", job.state === "completed");
    const stages = new Map(job.analysis.stages.map(s => [s.name, s]));
    for (const name of job.analysis.required_stages) check(`Required stage: ${name}`,
      ["complete", "written", "no_rows_flagged"].includes(stages.get(name)?.status));
    const sources = await api(`/cases/${caseId}/sources`);
    const { createHash } = await import("node:crypto");
    check("Uploaded source SHA-256 matches original bytes", sources.sources.some(s => s.sha256 === createHash("sha256").update(awaitedBytes).digest("hex")));
    findings = (await api(`/cases/${caseId}/findings?limit=200`)).findings;
    check("Stored findings exist", findings.length > 0);
    const queue = await api(`/cases/${caseId}/investigation-queue?capacity=100`);
    check("Durable grouping coverage is complete", queue.grouping_coverage?.state === "complete");
    group = queue.items.find(g => g.family.includes("peeling")) ?? queue.items[0];
    report.case_id = caseId; report.import_job = { state: job.state, analysis: job.analysis, rows_accepted: job.rows_accepted };
    report.queue = { underlying_findings: queue.underlying_findings, investigation_groups: queue.investigation_groups,
      queued_groups: queue.queued_groups, backlog_groups: queue.backlog_groups };
    await hold(1800);
    await page.goto(`${base}/cases/${caseId}/dashboard`);
    await page.locator(".stat-grid").waitFor(); await shot("dashboard.png"); await hold(2000);
  }, false);
  await scene("graph", async ({ page, click, shot }) => {
    const finding = findings.find(f => f.rule_id === "peeling_chain_candidate");
    check("Real peeling-chain finding exists for graph recording", Boolean(finding));
    await page.goto(`${base}/cases/${caseId}/graph`);
    await page.locator(".ff-picker-row").first().waitFor();
    await click(page.getByRole("button", { name: "Suspicious path", exact: true }));
    await page.locator(".path-list-row").first().waitFor();
    const peeling = page.locator(".path-list-row").filter({ hasText: "Peeling chain" }).first();
    await click(peeling); await hold(1800);
    await shot("graph.png");
    await click(page.getByTitle("Zoom out")); await hold(900);
    await click(page.getByTitle("Reset zoom"));
    await page.getByLabel("Trace a txid or address as the source").fill(finding.entity_ref);
    await click(page.getByRole("button", { name: "Trace", exact: true }));
    await page.locator(".ff-panel-title").waitFor(); await hold(2300);
    await page.getByLabel("Trace a txid or address as the source").fill(finding.entity_ref);
  });
  await scene("review", async ({ page, api, click, shot }) => {
    await page.goto(`${base}/cases/${caseId}/findings`);
    await page.getByLabel("Unresolved group capacity").waitFor(); await shot("groups.png");
    await page.getByLabel("Unresolved group capacity").fill("3"); await hold(1600);
    await page.getByLabel("Unresolved group capacity").fill("100"); await hold(800);
    await page.goto(`${base}/investigations/${group.group_id}`);
    await page.getByRole("heading", { name: "Review proposition" }).waitFor(); await hold(1700);
    const members = await api(`/investigation-groups/${group.group_id}/members?limit=20`);
    const finding = members.items[0].finding;
    await page.goto(`${base}/findings/${finding.finding_id}`);
    await page.getByRole("heading", { name: "Review Decision" }).waitFor(); await shot("evidence.png");
    await click(page.getByRole("button", { name: "View raw record" }).first());
    await page.locator(".source-ref-row pre").first().waitFor();
    check("Browser reopens the exact provenance-linked raw record", true); await hold(1800);
    await page.goto(`${base}/investigations/${group.group_id}`);
    await page.getByLabel("Recorded reason").fill("Synthetic demo: observed structure triaged for contextual review. No conclusion about identity or criminality.");
    const saving = page.waitForResponse(r => r.url().endsWith(`/investigation-groups/${group.group_id}/reviews`) && r.request().method() === "POST");
    await click(page.getByRole("button", { name: "Record group decision", exact: true }));
    check("Real API persists the group decision", (await saving).status() === 201);
    const membersAfter = await api(`/investigation-groups/${group.group_id}/members?limit=20`);
    check("Group decision preserves individual finding review state", membersAfter.items.every((m, i) => m.finding.review_state === members.items[i].finding.review_state));
    await hold(1400);
    await page.goto(`${base}/cases/${caseId}/network`);
    await page.getByRole("heading", { name: /Network Intelligence/ }).waitFor(); await shot("network.png"); await hold(1400);
    await page.goto(`${base}/cases/${caseId}/export`);
    await click(page.getByRole("button", { name: "Generate Export", exact: true }));
    await page.getByRole("heading", { name: "Evidence Export Preview" }).waitFor(); await shot("export.png");
    const downloading = page.waitForEvent("download");
    await click(page.getByRole("button", { name: "JSON", exact: true }).first());
    const download = await downloading;
    await download.saveAs(path.join(process.env.TRACEX_RECORD_WORK, "synthetic-export.json"));
    check("Real UI evidence download succeeds", (await readFile(path.join(process.env.TRACEX_RECORD_WORK, "synthetic-export.json"))).length > 0);
  });
  check("No external browser requests", report.external_requests.length === 0);
  check("No browser JavaScript exceptions", report.page_errors.length === 0);
  report.status = "pass";
} catch (error) {
  report.status = "failed"; report.error = error.stack; throw error;
} finally {
  await browser.close();
  await writeFile(path.join(output, "capture-manifest.json"), JSON.stringify(report, null, 2) + "\n");
}
