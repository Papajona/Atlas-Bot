import { chromium } from "playwright";
import fs from "node:fs/promises";
import path from "node:path";

const baseURL = "http://127.0.0.1:8765";
const outputDir = "ui-audit-artifacts/web";
await fs.mkdir(outputDir, { recursive: true });

const cases = [
  { name: "customer-desktop", route: "/", viewport: { width: 1440, height: 1000 } },
  { name: "customer-mobile", route: "/", viewport: { width: 390, height: 844 }, mobile: true },
  { name: "admin-login-desktop", route: "/admin", viewport: { width: 1440, height: 1000 }, adminAuthenticatedPreview: false },
  { name: "admin-login-mobile", route: "/admin", viewport: { width: 390, height: 844 }, mobile: true, adminAuthenticatedPreview: false },
  { name: "admin-workbench-desktop", route: "/admin", viewport: { width: 1440, height: 1000 }, adminAuthenticatedPreview: true },
  { name: "admin-workbench-mobile", route: "/admin", viewport: { width: 390, height: 844 }, mobile: true, adminAuthenticatedPreview: true },
];

const browser = await chromium.launch({ headless: true });
const manifest = [];
try {
  for (const item of cases) {
    const page = await browser.newPage({
      viewport: item.viewport,
      deviceScaleFactor: 1,
      colorScheme: "dark",
      reducedMotion: "reduce",
    });
    const consoleErrors = [];
    const pageErrors = [];
    page.on("console", msg => { if (msg.type() === "error") consoleErrors.push(msg.text()); });
    page.on("pageerror", err => pageErrors.push(String(err)));
    // This token is a synthetic UI fixture only and never leaves this local preview.
    if (item.route === "/admin" && item.adminAuthenticatedPreview) {
      await page.addInitScript(() => {
        sessionStorage.setItem("ait_admin_access_token", "ui-audit-synthetic-session");
      });
    }

    // The visual pass is isolated: local templates/static assets only, synthetic API replies only.
    await page.route("**/*", async route => {
      const url = new URL(route.request().url());
      if (!["127.0.0.1", "localhost"].includes(url.hostname)) return route.abort();
      // Local API requests are handled by ui_preview_server.py's endpoint-shaped fixtures.
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
        authScreenVisible: Boolean(document.querySelector("#auth") && getComputedStyle(document.querySelector("#auth")).display !== "none"),
        adminAuthPending: document.body.classList.contains("admin-auth-pending"),
        adminWorkbenchVisible: Boolean(document.querySelector("#strategy") && getComputedStyle(document.querySelector("#strategy")).display !== "none" && document.querySelector("#strategy").getClientRects().length),
        mobileNavVisible: Boolean(document.querySelector(".mobile-nav") && getComputedStyle(document.querySelector(".mobile-nav")).display !== "none" && document.querySelector(".mobile-nav").getClientRects().length),
        sidebarVisible: Boolean(document.querySelector(".sidebar") && getComputedStyle(document.querySelector(".sidebar")).display !== "none" && document.querySelector(".sidebar").getClientRects().length),
        topbarVisible: Boolean(document.querySelector(".topbar") && getComputedStyle(document.querySelector(".topbar")).display !== "none" && document.querySelector(".topbar").getClientRects().length),
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
    if (diagnostics.viewportWidth !== item.viewport.width) {
      throw new Error(`${item.name}: viewport mismatch, requested ${item.viewport.width}px but rendered ${diagnostics.viewportWidth}px`);
    }
    if (diagnostics.horizontalOverflow) {
      throw new Error(`${item.name}: horizontal overflow at ${diagnostics.viewportWidth}px: document width ${diagnostics.documentWidth}px`);
    }
    if (item.route === "/" && item.mobile && diagnostics.authScreenVisible && diagnostics.mobileNavVisible) {
      throw new Error(`${item.name}: mobile navigation overlaps the unauthenticated customer auth screen`);
    }
    if (item.route === "/admin" && item.adminAuthenticatedPreview === false && !diagnostics.adminAuthPending) {
      throw new Error(`${item.name}: unauthenticated admin preview unexpectedly exposed the control plane`);
    }
    if (item.route === "/admin" && item.adminAuthenticatedPreview === true && diagnostics.adminAuthPending) {
      throw new Error(`${item.name}: synthetic authenticated preview did not unlock the admin workbench`);
    }
    if (item.route === "/admin" && item.adminAuthenticatedPreview === false && diagnostics.adminWorkbenchVisible) {
      throw new Error(`${item.name}: admin workbench visible before session verification`);
    }
    if (item.route === "/admin" && item.adminAuthenticatedPreview === true && !diagnostics.adminWorkbenchVisible) {
      throw new Error(`${item.name}: admin workbench remained hidden after session fixture verification`);
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
