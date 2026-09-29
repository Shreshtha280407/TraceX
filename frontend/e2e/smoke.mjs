// Real end-to-end smoke test, driven by the real `playwright` npm package
// against a real Google Chrome binary (channel: "chrome"), navigating the
// actual running dev servers. No mocking, no simulated DOM.
//
// Prerequisite: both dev servers already running --
//   uv run uvicorn app.main:app --port 8001 --reload   (backend)
//   uv run tracex-worker                                (worker)
//   npm run dev                                          (frontend, :5173)
//
// Run:  npm run e2e
import { chromium } from "playwright";

const BASE = "http://localhost:5173";
const results = [];
let failed = 0;

function record(name, ok, detail = "") {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? " — " + detail : ""}`);
  if (!ok) failed++;
}

async function main() {
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const consoleErrors = [];
  const failedResponses = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text());
  });
  page.on("response", (res) => {
    if (res.status() >= 400) failedResponses.push(`${res.status()} ${res.url()}`);
  });

  const stamp = Date.now();
  const userName = `e2e-chrome-${stamp}`;
  const password = "correct-horse-battery-staple";
  const caseName = `E2E-CHROME-CASE-${stamp}`;

  try {
    // 1. Landing page
    await page.goto(BASE);
    record("landing page loads", (await page.title()).includes("TraceX"));
    const heroVisible = await page.locator(".landing-title").isVisible();
    record("landing hero visible", heroVisible);

    // 2. Open signup, feature sidebar present
    await page.locator(".landing-secondary").click();
    await page.waitForSelector(".auth-card");
    const featureCount = await page.locator(".auth-feature-list li").count();
    record("auth feature sidebar renders", featureCount >= 5, `${featureCount} features listed`);
    const noGoogleButton = (await page.locator(".auth-google").count()) === 0;
    record("no Google sign-in button (Firebase fully removed)", noGoogleButton);

    // 3. Sign up with name + password
    await page.fill("#name", userName);
    await page.fill("#password", password);
    await page.click(".auth-submit");

    // 4. Brand-new user -> lands on Home, New Case modal auto-opens
    await page.waitForURL("**/overview", { timeout: 10000 });
    record("brand-new signup lands on /overview (not a blocking onboarding page)", true);
    await page.waitForSelector(".modal-overlay", { timeout: 5000 });
    const modalTitle = await page.locator(".modal-header h2").textContent();
    record("New Case modal auto-opens on first sign-up", modalTitle?.includes("Create a new case") ?? false);
    const homeVisibleBehindModal = await page.locator("h1:has-text('Good evening')").isVisible();
    record("Home content visible behind the modal (background not blocked)", homeVisibleBehindModal);

    // 5. Dismiss it to "explore first"
    await page.locator(".modal-close").click();
    await page.waitForSelector(".modal-overlay", { state: "detached", timeout: 3000 });
    record("modal is dismissible via X (explore before creating a case)", true);

    // 6. Re-open via + New Case, create a real case
    await page.locator("button:has-text('+ New Case')").click();
    await page.waitForSelector(".modal-overlay");
    await page.fill("#new-case-name", caseName);
    await page.locator(".modal-actions button[type=submit]").click();
    await page.waitForSelector(".modal-overlay", { state: "detached", timeout: 5000 });
    await page.waitForSelector(`td:has-text('${caseName}')`, { timeout: 5000 });
    const caseRowVisible = await page.locator(`td:has-text('${caseName}')`).isVisible();
    record("created case appears immediately on Home (no navigation away)", caseRowVisible);

    // 7. Click into the case dashboard
    await page.locator(`td:has-text('${caseName}')`).click();
    await page.waitForURL("**/dashboard", { timeout: 5000 });
    record("clicking a case opens its dashboard", page.url().includes("/dashboard"));

    const viewGraphButtonGone = (await page.locator("button:has-text('View Graph')").count()) === 0;
    record("Dashboard has NO generic 'View Graph' button", viewGraphButtonGone);

    await page.waitForSelector(".case-switcher .themed-select-trigger", { timeout: 5000 });
    const caseSwitcherVisible = await page.locator(".case-switcher .themed-select-trigger").isVisible();
    record("case switcher dropdown present on a case-scoped page", caseSwitcherVisible);
    // The switcher shows the raw case ID as a placeholder for one render while
    // api.listCases() is in flight, then swaps to the friendly name -- wait for
    // that swap specifically, not just "the trigger has some text".
    await page.waitForFunction(
      (name) => document.querySelector(".case-switcher .themed-select-trigger")?.textContent?.includes(name),
      caseName,
      { timeout: 5000 }
    );
    const switcherShowsCaseName = (await page.locator(".case-switcher .themed-select-trigger").textContent())?.includes(caseName);
    record("case switcher shows the actual case name", switcherShowsCaseName ?? false);

    // No native <select> anywhere: Chromium can't restyle an open <select>'s
    // option list, so it always renders white regardless of CSS -- the whole
    // control was replaced with a custom-styled dropdown instead.
    const nativeSelectsGone = (await page.locator("select").count()) === 0;
    record("no native <select> elements remain (custom dropdown used instead, fully themed)", nativeSelectsGone);

    const pendingReviewHeading = await page.locator("h2:has-text('Pending Review')").isVisible();
    record("Dashboard shows Pending Review section", pendingReviewHeading);

    // 8. Sidebar has no "Model & Rules" icon anymore
    const modelRulesIconGone = (await page.locator("a[title='Model & Rules']").count()) === 0;
    record("'Model & Rules' nav icon removed from sidebar", modelRulesIconGone);

    // 9. Navigate to Graph Explorer, exercise the combobox
    await page.locator("a[title='UTXO Graph Explorer']").click();
    await page.waitForURL("**/graph", { timeout: 5000 });
    record("Graph Explorer reachable from sidebar", page.url().includes("/graph"));

    const seedInput = page.locator(".combobox input");
    await seedInput.click();
    await page.waitForSelector(".combobox-panel", { timeout: 3000 });
    const pillsVisible = await page.locator(".combobox-panel .pill").count();
    record("Graph seed combobox shows filter pills when opened", pillsVisible === 3, `${pillsVisible} pills`);
    const emptyStateText = await page.locator(".combobox-panel .coverage-note").textContent().catch(() => null);
    record(
      "Graph combobox shows an honest empty state for a case with no findings yet",
      emptyStateText?.includes("No matching findings") ?? false
    );
    await page.keyboard.press("Escape");

    // 10. Sidebar has no "Case Settings" icon anymore either -- merged into the
    // global Settings page reached via the avatar circle.
    const caseSettingsIconGone = (await page.locator("a[title='Case Settings']").count()) === 0;
    record("'Case Settings' nav icon removed from sidebar (merged into global Settings)", caseSettingsIconGone);

    // 11. Avatar circle -> global Settings page (not an immediate logout)
    await page.locator(".avatar-badge").click();
    await page.waitForURL("**/settings", { timeout: 5000 });
    const onGlobalSettings = !page.url().includes("/cases/");
    record("avatar circle opens global /settings (not case-scoped, not instant logout)", onGlobalSettings);

    await page.waitForSelector("h2:has-text('About TraceX')", { timeout: 5000 });
    const aboutVisible = await page.locator("h2:has-text('About TraceX')").isVisible();
    const caseSettingsVisible = await page.locator("h2:has-text('Case Settings')").isVisible();
    const modelVisible = await page.locator("h2:has-text('Deployed Model')").isVisible();
    const comparisonVisible = await page.locator("h2:has-text('Comparison')").isVisible();
    const accountVisible = await page.locator("h2:has-text('Account')").isVisible();
    const dataSourcesVisible = await page.locator("h2:has-text('Data Sources')").isVisible();
    const systemCheckVisible = await page.locator("h2:has-text('System Check')").isVisible();
    record("Settings has About TraceX section", aboutVisible);
    record("Settings has merged-in Case Settings section", caseSettingsVisible);
    record("Settings has Deployed Model section", modelVisible);
    record("Settings has holdout Comparison table", comparisonVisible);
    record("Settings has Account section", accountVisible);
    record("Settings has merged-in Data Sources section", dataSourcesVisible);
    record("Settings has System Check section", systemCheckVisible);

    const accessRolesGone = (await page.locator("h2:has-text('Access & Roles')").count()) === 0;
    record("'Access & Roles' section removed (not carried into the merge)", accessRolesGone);
    const dangerZoneGone = (await page.locator("h2:has-text('Danger Zone')").count()) === 0;
    record("'Danger Zone' section removed (not carried into the merge)", dangerZoneGone);

    // The merged-in Case Settings picker should already show the case we just
    // created and navigated through -- confirms the picker seeds itself from
    // the last-visited case rather than starting on an unrelated one.
    const caseSettingsCard = page.locator(".neo", { has: page.locator("h2:has-text('Case Settings')") });
    await caseSettingsCard.locator(".kv-row", { hasText: caseName }).first().waitFor({ timeout: 5000 });
    const casePickerLabel = await page.locator("button[aria-label='Choose a case']").textContent();
    record(
      "merged Case Settings defaults to the last-visited case",
      true,
      `picker label: ${casePickerLabel}`
    );

    // The actual bug report: the case picker's open panel must be dark-themed,
    // not the browser's native white option list.
    await page.locator("button[aria-label='Choose a case']").click();
    await page.waitForSelector(".themed-select-panel", { timeout: 3000 });
    const panelBg = await page.locator(".themed-select-panel").evaluate((el) => getComputedStyle(el).backgroundColor);
    const isDarkPanel = /rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(panelBg)?.slice(1, 4).map(Number).every((c) => c < 60) ?? false;
    record("case picker's open dropdown panel is dark-themed, not a native white list", isDarkPanel, panelBg);
    await page.keyboard.press("Escape");

    // 12. Run the live system check against the real backend
    await page.locator("button:has-text('Run system check')").click();
    await page.waitForSelector(".badge:has-text('PASS'), .badge:has-text('FAIL')", { timeout: 8000 });
    const passCount = await page.locator(".badge:has-text('PASS')").count();
    const failCount = await page.locator(".badge:has-text('FAIL')").count();
    record("live system check hits the real backend and reports a result", passCount + failCount >= 2, `${passCount} PASS, ${failCount} FAIL`);
    record("live system check: backend is actually healthy", passCount === 2 && failCount === 0);

    // 13. Sign out from Settings
    await page.locator("button:has-text('Sign out')").click();
    await page.waitForURL(BASE + "/", { timeout: 5000 });
    await page.waitForSelector(".landing-primary", { timeout: 5000 });
    const backOnLanding = await page.locator(".landing-primary").isVisible();
    record("Sign out returns to the landing page", backOnLanding);

    // 14. Confirm the session is really gone (protected route redirects)
    await page.goto(`${BASE}/overview`);
    await page.waitForURL(BASE + "/", { timeout: 5000 });
    record("Visiting a protected route after sign-out redirects to landing", true);

    console.log("\n--- failed HTTP responses seen during the run ---");
    failedResponses.forEach((f) => console.log(" ", f));
    const unexplained404s = failedResponses.filter((f) => f.startsWith("404") && !f.includes("favicon.ico"));
    record(
      "no unexplained 404s (favicon.ico is a known pre-existing gap)",
      unexplained404s.length === 0,
      unexplained404s.slice(0, 3).join(" | ")
    );
    const fontCdnErrors = consoleErrors.filter((e) => e.includes("gstatic") || e.includes("NETWORK_CHANGED"));
    if (fontCdnErrors.length > 0) {
      record(
        "FINDING: tokens.css loads Google Fonts from an external CDN — offline-unfriendly",
        false,
        `${fontCdnErrors.length} font fetch(es) failed under network isolation`
      );
    }
  } catch (err) {
    record("UNEXPECTED EXCEPTION", false, err.message);
    await page.screenshot({ path: "/tmp/claude-1000/-home-Shreshtha-documents-TraceX/f566397b-929f-471b-b770-046f705bcf89/scratchpad/e2e-failure.png" }).catch(() => {});
  } finally {
    await browser.close();
  }

  console.log("\n=== SUMMARY ===");
  console.log(`${results.length - failed}/${results.length} passed`);
  if (failed > 0) process.exit(1);
}

main();
