import { chromium } from "playwright";
import fs from "node:fs/promises";
import path from "node:path";

const baseURL = "http://127.0.0.1:8765";
const outputDir = "ui-audit-artifacts/web";
await fs.mkdir(outputDir, { recursive: true });

const cases = [
  { name: "customer-desktop", route: "/", viewport: { width: 1440, height: 1000 } },
  { name: "customer-mobile", route: "/", viewport: { width: 390, height: 844 }, mobile: true },
  { name: "admin-desktop", route: "/admin", viewport: { width: 1440, height: 1000 } },
  { name: "admin-mobile", route: "/admin", viewport: { width: 390, height: 844 }, mobile: true },
];

const browser = await chromium.launch({ headless: true });
const manifest = [];
try {
  for (const item of cases) {
    const page = await browser.newPage({
      viewport: item.viewport,
      deviceScaleFactor: 1,
      isMobile: Boolean(item.mobile),
      hasTouch: Boolean(item.mobile),
      colorScheme: "dark",
      reducedMotion: "reduce",
    });
    const consoleErrors = [];
    const pageErrors = [];
    page.on("console", msg => { if (msg.type() === "error") consoleErrors.push(msg.text()); });
    page.on("pageerror", err => pageErrors.push(String(err)));

    // The visual pass is isolated: local templates/static assets only, synthetic API replies only.
    await page.route("**/*", async route => {
      const url = new URL(route.request().url());
      if (!["127.0.0.1", "localhost"].includes(url.hostname)) return route.abort();
      if (url.pathname.startsWith("/api/")) {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ ok: true, preview: true, status: "PAPER", items: [], data: [] }),
        });
      }
      return route.continue();
    });

    const response = await page.goto(baseURL + item.route, { waitUntil: "domcontentloaded", timeout: 15000 });
    if (!response || response.status() !== 200) {
      throw new Error(`${item.name}: page load failed (HTTP ${response?.status() ?? "no response"})`);
    }
    await page.waitForTimeout(900);
    const diagnostics = await page.evaluate(() => {
      const doc = document.documentElement;
      const body = document.body;
      const styleSheets = Array.from(document.styleSheets)
        .map(sheet => sheet.href)
        .filter(Boolean);
      return {
        title: document.title,
        viewportWidth: window.innerWidth,
        documentWidth: doc.scrollWidth,
        bodyWidth: body.scrollWidth,
        viewportHeight: window.innerHeight,
        documentHeight: Math.max(doc.scrollHeight, body.scrollHeight),
        horizontalOverflow: doc.scrollWidth > window.innerWidth + 1,
        mainHeadings: Array.from(document.querySelectorAll("h1, h2"))
          .slice(0, 18).map(el => (el.innerText || "").trim()).filter(Boolean),
        styleSheets,
        visibleButtons: Array.from(document.querySelectorAll("button"))
          .filter(el => el.getClientRects().length).length,
        visibleInputs: Array.from(document.querySelectorAll("input, select, textarea"))
          .filter(el => el.getClientRects().length).length,
      };
    });
    if (!diagnostics.title || diagnostics.documentHeight < 200) {
      throw new Error(`${item.name}: rendered document appears incomplete: ${JSON.stringify(diagnostics)}`);
    }
    const screenshot = path.join(outputDir, `${item.name}.png`);
    await page.screenshot({ path: screenshot, fullPage: true, animations: "disabled" });
    manifest.push({
      ...item,
      screenshot,
      httpStatus: response.status(),
      ...diagnostics,
      consoleErrors: consoleErrors.slice(0, 30),
      pageErrors: pageErrors.slice(0, 30),
    });
    await page.close();
  }
} finally {
  await browser.close();
}
await fs.writeFile(path.join(outputDir, "manifest.json"), JSON.stringify({
  note: "Screenshots render repository templates with synthetic settings and stub APIs. They are not captures of the deployed staging service.",
  capturedAt: new Date().toISOString(),
  screens: manifest,
}, null, 2));
console.log(JSON.stringify(manifest, null, 2));
