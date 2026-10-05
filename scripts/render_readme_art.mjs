// Render code-native README artwork with the same local fonts as the product.
import { chromium } from "../frontend/node_modules/playwright/index.mjs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { mkdir } from "node:fs/promises";
import { spawnSync } from "node:child_process";
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const output = path.join(root, "docs/assets/readme");
const temporary = "/tmp/tracex-readme-art";
await mkdir(temporary, { recursive: true });
const browser = await chromium.launch({ channel: "chrome", headless: true });
try {
  const context = await browser.newContext({ viewport: { width: 1400, height: 520 } });
  const page = await context.newPage();
  await page.goto(`file://${output}/hero.html?fixed`);
  await page.evaluate(() => document.fonts.ready);
  await page.evaluate(() => window.paint(3000));
  await page.screenshot({ path: path.join(output, "hero.png") });
  for (let i = 0; i < 120; i++) {
    await page.evaluate(t => window.paint(t), i * 100);
    await page.screenshot({ path: path.join(temporary, `hero-${String(i).padStart(3, '0')}.png`) });
  }
  await context.close();
  const converted = spawnSync("ffmpeg", ["-hide_banner", "-loglevel", "error", "-y", "-framerate", "10", "-i", path.join(temporary, "hero-%03d.png"),
    "-t", "12", "-vf", "scale=1200:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=96[p];[s1][p]paletteuse=dither=none:diff_mode=rectangle",
    "-loop", "0", path.join(output, "hero.gif")], { stdio: "inherit" });
  if (converted.status !== 0) throw new Error("Hero GIF conversion failed");
  console.log("Rendered hero GIF and static poster.");
  const previewIndex = process.argv.indexOf("--preview");
  if (previewIndex >= 0) {
    const preview = path.resolve(process.argv[previewIndex + 1]);
    const inspection = await browser.newContext({ viewport: { width: 1280, height: 1000 } });
    const rendered = await inspection.newPage();
    await rendered.goto(`file://${preview}`);
    await rendered.evaluate(() => document.fonts.ready);
    const broken = await rendered.evaluate(() => Array.from(document.images)
      .filter(img => !img.complete || img.naturalWidth === 0).map(img => img.src));
    if (broken.length) throw new Error(`Broken preview images: ${broken.join(", ")}`);
    await rendered.screenshot({ path: path.join(temporary, "readme-top.png") });
    await rendered.getByRole("heading", { name: "How it works", exact: true }).scrollIntoViewIfNeeded();
    await rendered.screenshot({ path: path.join(temporary, "readme-architecture.png") });
    await rendered.getByRole("heading", { name: "Documentation", exact: true }).scrollIntoViewIfNeeded();
    await rendered.screenshot({ path: path.join(temporary, "readme-docs.png") });
    await inspection.close();
    console.log("README preview inspected; all images loaded.");
  }
} finally { await browser.close(); }
