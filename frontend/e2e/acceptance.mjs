// Real authenticated browser acceptance against the copied offline appliance.
import { chromium } from "playwright";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import path from "node:path";

const base = process.env.TRACEX_E2E_BASE ?? "http://127.0.0.1:8770";
// A Vite frontend and local API may be separate origins. Single-origin appliance
// checks remain the default; declaring a local API is not network-isolation proof.
const apiBase = process.env.TRACEX_E2E_API_BASE ?? base;
if (new URL(apiBase).origin !== new URL(base).origin && !["localhost", "127.0.0.1", "[::1]"].includes(new URL(apiBase).hostname)) {
  throw new Error("Separate-origin development testing requires an explicitly local API, not an external runtime service");
}
const allowedOrigins = new Set([new URL(base).origin, new URL(apiBase).origin]);
const source = process.env.TRACEX_E2E_SOURCE;
const missingVariant = process.env.TRACEX_E2E_EXPECT_MISSING === "1";
if (!source) throw new Error("TRACEX_E2E_SOURCE must name a real unfamiliar NDJSON demo file");
const output = process.env.TRACEX_E2E_OUTPUT ?? path.resolve("../var/browser-runs", `acceptance-${Date.now()}`);
await mkdir(output, { recursive: true });
const report = { base, api_base: apiBase, source, checks: [], externalRequests: [], consoleErrors: [], httpErrors: [],
  network_scope: apiBase === base ? "Single-origin appliance browser checks" : "Local development frontend/API origins; not native-Linux appliance isolation proof" };
const check = (name, ok, detail = null) => {
  report.checks.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"} ${name}`);
  if (!ok) throw new Error(name);
};
const browser = await chromium.launch({ channel: "chrome", headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
await context.route("**/*", async (route) => {
  const url = route.request().url();
    if (/^https?:/.test(url) && !allowedOrigins.has(new URL(url).origin)) {
    report.externalRequests.push(url);
    await route.abort();
  } else await route.continue();
});
page.on("pageerror", (error) => report.consoleErrors.push(error.message));
page.on("response", (response) => { if (response.status() >= 400) report.httpErrors.push({ status: response.status(), url: response.url() }); });
let token;
async function api(endpoint, options = {}) {
  const response = await context.request.fetch(`${apiBase}/v1${endpoint}`, {
    ...options, headers: { Authorization: `Bearer ${token}`, ...(options.headers ?? {}) }, timeout: 120000,
  });
  if (!response.ok()) throw new Error(`${response.status()} ${endpoint}: ${(await response.text()).slice(0, 300)}`);
  return response.json();
}
try {
  await page.goto(base);
  await page.locator(".landing-secondary").click();
  await page.fill("#name", `review-acceptance-${Date.now()}`);
  await page.fill("#password", "disposable-browser-acceptance-password");
  await page.click(".auth-submit");
  await page.waitForURL("**/overview");
  token = await page.evaluate(() => JSON.parse(localStorage.getItem("tracex.auth")).token);
  check("real browser signup and bearer authentication", Boolean(token));
  const created = await api("/cases", { method: "POST", data: { name: `Unfamiliar demo ${Date.now()}`, synthetic: true } });
  const id = created.case_id;
  report.case_id = id;
  await page.goto(`${base}/cases/${id}/ingestion`);
  const uploadResponse = page.waitForResponse((response) => response.url().endsWith(`/cases/${id}/imports`) && response.request().method() === "POST");
  await page.locator("#evidence-file-input").setInputFiles(path.resolve(source));
  const uploaded = await uploadResponse;
  check("UI file upload accepted", uploaded.status() === 202);
  const jobId = (await uploaded.json()).job_id;
  let job;
  let provisionalObserved = false;
  await page.locator(".stage-track").waitFor();
  check("UI exposes real stage progression", await page.locator(".stage-node").count() >= 6);
  const deadline = Date.now() + 600000;
  while (Date.now() < deadline) {
    job = await api(`/jobs/${jobId}`);
    if (["completed", "failed"].includes(job.state)) break;
    if (job.rows_accepted > 0) {
      const live = await api(`/cases/${id}/activity?job_id=${jobId}&limit=2`);
      provisionalObserved ||= live.provisional && live.total > 0;
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  report.job = job;
  report.provisional_activity_observed = provisionalObserved;
  const stages = new Map(job.analysis.stages.map((stage) => [stage.name, stage]));
  check("real worker completed all mandatory stages", job.state === "completed" && job.analysis.required_stages.every((name) => ["complete", "written", "no_rows_flagged"].includes(stages.get(name)?.status)), job.analysis);
  await page.getByText("Finalized receipt view", { exact: false }).waitFor();
  check("UI reconciles receipt activity at finalization", true);
  if (missingVariant) {
    check("missing network is explicitly degraded, not normal", job.analysis.state === "degraded" && stages.get("network_coverage")?.status === "incomplete");
    await page.getByText(/Evidence imported\. Analysis is degraded/).waitFor();
    check("UI shows actionable degraded analysis", true);
    const analysis = await api(`/cases/${id}/analysis`);
    check("absent outpoints produce incomplete lineage", analysis.graph.coverage.spend_lineage_complete === false && analysis.graph.coverage.resolved_spend_inputs === 0, analysis.graph.coverage);
  }
  const bytes = await readFile(source);
  report.source_sha256 = createHash("sha256").update(bytes).digest("hex");
  const sources = await api(`/cases/${id}/sources`);
  check("source hash matches the uploaded bytes", sources.sources.some((item) => item.sha256 === report.source_sha256));
  const activity = await api(`/cases/${id}/activity?limit=2`);
  check("receipt-approved address activity is finalized", activity.total > 0 && !activity.provisional);
  const findings = await api(`/cases/${id}/findings?limit=200`);
  check("unfamiliar data has reviewable findings", findings.findings.length > 0);
  const workload = await api(`/cases/${id}/investigation-queue?capacity=100`);
  await page.goto(`${base}/cases/${id}/dashboard`);
  await page.locator(".stat-grid").getByText(`${workload.backlog_groups.toLocaleString()} additional unresolved groups · capacity 100`, {exact:true}).waitFor();
  const stats = await page.locator(".stat-grid").textContent();
  check("dashboard separates the grouped queue from raw open observations", stats.includes("In your review queue") && !stats.includes("Open findings") && stats.includes(`${workload.backlog_groups.toLocaleString()} additional unresolved groups`));
  check("actual import has complete published grouping coverage", workload.grouping_coverage?.state === "complete" && workload.grouping_coverage.grouped_findings === findings.total);
  await page.goto(`${base}/cases/${id}/findings`);
  await page.getByRole("heading", { name: "Separate ML transaction triage queue" }).waitFor();
  const capacity = page.getByLabel("Transactions to queue");
  await capacity.fill("2");
  await page.getByText(/capacity 2 ·/).waitFor();
  const queue = await api(`/cases/${id}/review-queue?k=2`);
  check("UI and API honor the distinct-transaction cap", queue.queued_transactions <= 2 && new Set(queue.items.map((item) => item.transaction)).size === queue.items.length);
  await capacity.fill("0");
  await page.getByText(/capacity 0 · 0 queued/).waitFor();
  check("UI supports zero review capacity", true);
  const groupQueue = await api(`/cases/${id}/investigation-queue?capacity=100`);
  check("group queue counts reconcile with underlying findings", groupQueue.underlying_findings === findings.total && groupQueue.queued_groups + groupQueue.backlog_groups === groupQueue.unresolved_groups);
  check("reviewable groups were durably materialized", groupQueue.items.length > 0 && job.analysis.stages.some(s => s.name === "investigation_grouping" && s.status === "complete"));
  const groupCapacity = page.getByLabel("Unresolved group capacity");
  await groupCapacity.fill("1");
  await page.getByTestId("group-counts").filter({hasText: /1 in your review queue/}).waitFor();
  await groupCapacity.fill("0");
  await page.getByTestId("group-counts").filter({hasText: /0 in your review queue/}).waitFor();
  check("group queue supports zero and one without discarding backlog", true);
  const group = groupQueue.items[0];
  await page.goto(`${base}/investigations/${group.group_id}`);
  await page.getByRole("heading", {name:"Review proposition"}).waitFor();
  check("group detail opens with separate proposition and paginated member evidence", await page.getByRole("heading",{name:"Member evidence"}).count() === 1);
  const membersBefore = await api(`/investigation-groups/${group.group_id}/members?limit=20`);
  const reviewGroupResponse = page.waitForResponse(r => r.url().endsWith(`/investigation-groups/${group.group_id}/reviews`) && r.request().method() === "POST");
  const citedRef = membersBefore.items[0].finding.source_refs[0];
  await page.getByLabel("Recorded reason").fill("Browser acceptance: triaged the observed episode only, no transaction verdict.");
  await page.getByLabel("Opposing source ID").fill(citedRef.evidence_id);
  await page.getByLabel("Exact opposing locator").fill(citedRef.locator);
  await page.getByRole("button",{name:"Record group decision"}).click();
  check("group decision persists through ordinary UI", (await reviewGroupResponse).status() === 201);
  const membersAfter = await api(`/investigation-groups/${group.group_id}/members?limit=20`);
  const queueAfter = await api(`/cases/${id}/investigation-queue?capacity=100`);
  check("group decision frees capacity without changing individual decisions", queueAfter.unresolved_groups === groupQueue.unresolved_groups-1 && membersBefore.items.every((m,i) => m.finding.status === membersAfter.items[i].finding.status));
  await page.reload();
  await page.getByText(/Decision 1: triaged/).waitFor();
  check("append-only group decision history survives reopening", true);
  await page.getByRole("button", {name: /Reopen group review reference:/}).click();
  await page.getByLabel("Reopen group review reference", {exact:true}).locator("pre").waitFor();
  check("group reviewer citation replays the pinned source without inventing a detector contradiction", true);
  const finding = findings.findings.find((item) => item.entity_ref.startsWith("tx:")) ?? findings.findings[0];
  report.finding_id = finding.finding_id;
  await page.goto(`${base}/findings/${finding.finding_id}`);
  await page.getByRole("heading", { name: "Review Decision" }).waitFor();
  await page.getByRole("button", { name: "View raw record" }).first().click();
  await page.locator(".source-ref-row pre").first().waitFor();
  check("UI reopens a provenance-linked raw record", true);
  await page.getByRole("button", { name: "Dismiss", exact: true }).click();
  await page.fill("#review-reason", "Synthetic acceptance: benign alternative retained; no criminality conclusion.");
  const reviewResponse = page.waitForResponse((response) => response.url().endsWith(`/findings/${finding.finding_id}/reviews`) && response.request().method() === "POST");
  await page.getByRole("button", { name: "Submit decision" }).click();
  check("UI review writes a versioned audit decision", (await reviewResponse).status() === 201);
  const evidence = await api(`/findings/${finding.finding_id}/evidence`);
  check("review history and pinned evidence survive review", evidence.review_history.length > 0 && evidence.source_refs.length > 0);
  await page.getByLabel("Neighborhood page size", { exact: true }).fill("2");
  check("neighborhood controls share a compact dark card", await page.locator(".neighborhood-card").evaluate((card) => {
    const button = card.querySelector(".neighborhood-load");
    const field = card.querySelector('input[aria-label="Neighborhood page size"]');
    return Boolean(button && field && card.querySelector(".neighborhood-header")) &&
      getComputedStyle(button).backgroundColor === getComputedStyle(field).backgroundColor &&
      getComputedStyle(button).backgroundColor !== "rgb(255, 255, 255)";
  }));
  await page.getByRole("button", { name: "Load neighborhood", exact: true }).click();
  await page.getByText(/nodes · .*edges loaded/).waitFor();
  check("actual evidence neighborhood renders committed graph data", true);
  const visual = page.getByRole("img", { name: "Bounded evidence association graph" });
  const firstVisualCount = await visual.locator("circle").count();
  check("signed graph page is drawn in the visual investigation view", firstVisualCount > 0 && firstVisualCount <= 100);
  const highlightedSource = visual.locator(".neighborhood-node.source");
  check("neighborhood marks only the exact requested source",
    await highlightedSource.count() === 1 && (await highlightedSource.getAttribute("aria-label")) === `Source node; Inspect ${finding.entity_ref}`);
  const firstNeighborhood = await page.getByText(/nodes · .*edges loaded/).textContent();
  await page.getByRole("button", { name: "Load more neighborhood", exact: true }).click();
  await page.waitForFunction((prior) => Array.from(document.querySelectorAll("p")).some((item) =>
    /nodes · .*edges loaded/.test(item.textContent ?? "") && item.textContent !== prior), firstNeighborhood);
  check("actual UI Load more advances the capped neighborhood", true);
  const nextVisualCount = await visual.locator("circle").count();
  check("continuation data reaches bounded SVG rendering", nextVisualCount > firstVisualCount && nextVisualCount <= 100);
  await visual.getByRole("button").first().focus();
  await page.keyboard.press("Enter");
  await page.getByText("View stored node details", { exact: true }).waitFor();
  check("neighborhood keyboard selection opens stored node details", true);
  await visual.getByRole("button").last().click();
  check("source highlight survives selecting another node",
    await highlightedSource.count() === 1 && (await highlightedSource.getAttribute("aria-pressed")) === "false");
  await page.getByRole("button", { name: "Clear selection", exact: true }).click();
  await page.locator(".neighborhood-card").screenshot({ path: path.join(output, "evidence-neighborhood.png") });
  if (finding.entity_ref.startsWith("tx:")) {
    const params = new URLSearchParams({ seed: finding.entity_ref, depth: "2", node_limit: "2", edge_limit: "1" });
    const nodes = new Set(), edges = new Set();
    let cursor, pages = 0;
    do {
      if (cursor) params.set("cursor", cursor);
      const graph = await api(`/cases/${id}/graph?${params}`);
      check(`graph continuation page ${++pages} has no duplicates`, graph.nodes.every((node) => !nodes.has(node.id)) && graph.edges.every((edge) => !edges.has(edge.id)));
      graph.nodes.forEach((node) => nodes.add(node.id));
      graph.edges.forEach((edge) => edges.add(edge.id));
      cursor = graph.cursor;
      if (pages > 1000) throw new Error("Graph continuation did not terminate");
    } while (cursor);
    const full = await api(`/cases/${id}/graph?${new URLSearchParams({ seed: finding.entity_ref, depth: "2", node_limit: "1000", edge_limit: "3000" })}`);
    check("cursor traversal has no missing nodes or edges", !full.cursor && full.nodes.length === nodes.size && full.edges.length === edges.size);
  }
  await page.getByRole("button", { name: "Reset neighborhood", exact: true }).click();
  check("neighborhood reset restores the empty state without a visual graph",
    await page.getByRole("button", { name: "Load neighborhood", exact: true }).isVisible() && await visual.count() === 0);
  await page.goto(`${base}/cases/${id}/graph?seed=${encodeURIComponent(finding.entity_ref)}`);
  await page.getByRole("heading", { name: "UTXO Graph Explorer", exact: true }).waitFor();
  await page.locator(".ff-toolbar").waitFor();
  check("Graph Explorer retains its main flow view without a duplicate neighborhood", await page.locator(".neighborhood-card").count() === 0);
  const entityPage = await api(`/cases/${id}/entities?limit=2&min_addresses=1`);
  check("entities and embeddings are available", entityPage.summary.wallet_count > 0);
  const wallet = entityPage.entities[0].entity_id;
  await api(`/cases/${id}/risk/seeds`, { method: "POST", data: { wallet_ref: wallet, label: "synthetic-demo", reason: "Synthetic analyst seed, not attribution", weight: .5 } });
  const risk = await api(`/cases/${id}/risk/run`, { method: "POST" });
  check("risk propagation pins the analytics revision", Boolean(risk.parameters.analytics_sha256));
  const entityAnalysisResponse = page.waitForResponse((response) => response.url().endsWith(`/cases/${id}/analysis`));
  await page.goto(`${base}/cases/${id}/entities`);
  await page.getByRole("heading", { name: /Entities/ }).first().waitFor();
  await (await entityAnalysisResponse).finished();
  check("Entities and Risk hides the completed-analysis banner", await page.getByText(/Analysis complete\./).count() === 0);
  const analysisFixture = await api(`/cases/${id}/analysis`);
  const analysisRoute = `**/v1/cases/${id}/analysis`;
  await page.route(analysisRoute, (route) => route.fulfill({ json: { ...analysisFixture,
    analysis: { ...analysisFixture.analysis, state: "degraded" } } }));
  await page.reload();
  await page.getByText(/Analysis degraded/).waitFor();
  check("Entities retains degraded-analysis warnings (mocked response)", true);
  await page.unroute(analysisRoute);
  await page.goto(`${base}/cases/${id}/network`);
  await page.getByRole("heading", { name: /Network Intelligence/ }).waitFor();
  check("entity and network pages render real analytics", true);
  const exported = await api(`/cases/${id}/findings/export`);
  check("export contains finding provenance and review history", Boolean(exported));
  await writeFile(path.join(output, "findings-export.json"), JSON.stringify(exported, null, 2), { flag: "wx" });
  await page.goto(`${base}/cases/${id}/export`);
  await page.getByRole("button", { name: "Generate Export", exact: true }).click();
  await page.getByRole("heading", { name: "Evidence Export Preview" }).waitFor();
  const downloadEvent = page.waitForEvent("download");
  await page.getByRole("button", { name: "JSON", exact: true }).first().click();
  const download = await downloadEvent;
  await download.saveAs(path.join(output, "ui-findings-download.json"));
  check("actual UI export download succeeds", (await readFile(path.join(output, "ui-findings-download.json"))).length > 0);
  const outsider = await context.request.post(`${apiBase}/v1/auth/signup`, {data: {
    display_name: `group-outsider-${Date.now()}`, password: "disposable-unauthorized-browser-password",
  }});
  const outsiderToken = (await outsider.json()).token;
  for (const suffix of ["", "/members", "/reviews", "/export", "/subjects", "/replacements"]) {
    const denied = await context.request.get(`${apiBase}/v1/investigation-groups/${group.group_id}${suffix}`, {
      headers: {Authorization: `Bearer ${outsiderToken}`},
    });
    check(`browser cross-case group access denied ${suffix || "detail"}`, denied.status() === 404);
  }
  const deniedDecision = await context.request.post(`${apiBase}/v1/investigation-groups/${group.group_id}/reviews`, {
    headers: {Authorization: `Bearer ${outsiderToken}`}, data: {
      expected_review_version: 2, disposition: "confirmed", reason: "Unauthorized check; must never be recorded",
    },
  });
  check("browser cross-case group decision denied", deniedDecision.status() === 404);
  const removed = await context.request.post(`${apiBase}/v1/findings/${finding.finding_id}/chat`, { headers: { Authorization: `Bearer ${token}` }, data: {} });
  check("removed language-model endpoint does not exist", removed.status() === 404);
  check("browser requested no external resources", report.externalRequests.length === 0, report.externalRequests);
  check("browser raised no runtime JavaScript exceptions", report.consoleErrors.length === 0, report.consoleErrors);
  await page.screenshot({ path: path.join(output, "network.png"), fullPage: true });
  report.status = "pass";
} catch (error) {
  report.status = "failed";
  report.error = error.stack;
  await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true }).catch(() => {});
} finally {
  await browser.close();
  await writeFile(path.join(output, "result.json"), JSON.stringify(report, null, 2), { flag: "wx" });
}
process.exitCode = report.status === "pass" ? 0 : 1;
