const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");

const ROOT = process.cwd();
const ADMIN_CSP = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'";
const CUSTOMER_CSP = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'";

function send(res, status, type, body, headers = {}) {
  res.writeHead(status, { "content-type": type, "x-content-type-options": "nosniff", ...headers });
  res.end(body);
}
function json(route, value, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(value) });
}

const server = http.createServer((req, res) => {
  const pathname = new URL(req.url, "http://127.0.0.1").pathname;
  if (pathname === "/admin" || pathname === "/admin/") {
    return send(res, 200, "text/html; charset=utf-8", fs.readFileSync(path.join(ROOT, "app/templates/dashboard.html")), {
      "content-security-policy": ADMIN_CSP,
    });
  }
  if (pathname === "/customer" || pathname === "/customer/") {
    return send(res, 200, "text/html; charset=utf-8", fs.readFileSync(path.join(ROOT, "app/templates/customer.html")), {
      "content-security-policy": CUSTOMER_CSP,
    });
  }
  if (pathname.startsWith("/static/")) {
    const relative = pathname.slice("/static/".length);
    if (!relative || relative.includes("..") || path.isAbsolute(relative)) {
      return send(res, 400, "text/plain; charset=utf-8", "Bad static path");
    }
    const fullPath = path.join(ROOT, "app/static", relative);
    if (!fs.existsSync(fullPath) || !fs.statSync(fullPath).isFile()) {
      return send(res, 404, "text/plain; charset=utf-8", "Static file not found");
    }
    const ext = path.extname(fullPath);
    const types = { ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml" };
    return send(res, 200, types[ext] || "application/octet-stream", fs.readFileSync(fullPath));
  }
  return send(res, 404, "text/plain; charset=utf-8", "Not found");
});

const waitUntil = async (predicate, timeoutMs = 10000, stepMs = 25) => {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (await predicate()) return;
    await new Promise(resolve => setTimeout(resolve, stepMs));
  }
  throw new Error("Timed out while waiting for browser test condition");
};

async function testAdminDashboard(browser, baseUrl) {
  const page = await browser.newPage({ viewport: { width: 1365, height: 900 } });
  const pageErrors = [];
  page.on("pageerror", error => pageErrors.push(error.message));
  await page.addInitScript(({ accessToken }) => {
    sessionStorage.setItem("ait_admin_access_token", accessToken);
    sessionStorage.setItem("ait_admin_mfa_verified", "true");
    window.__cspViolations = [];
    window.addEventListener("securitypolicyviolation", event => {
      window.__cspViolations.push(`${event.violatedDirective}: ${event.blockedURI}`);
    });
  }, { accessToken: "e30." + Buffer.from(JSON.stringify({ sub: "admin-user-1", aal: "aal2" })).toString("base64url") + ".test-signature" });

  const digest = "a".repeat(64);
  const rows = Array.from({ length: 100 }, (_, index) => ({
    id: index + 1,
    request_id: `wd-browser-${index + 1}`,
    customer_id: index + 900,
    customer_email: `customer${index + 1}@example.invalid`,
    account_ref: `acct-${index + 1}`,
    amount: index === 0 ? 12.345678 : 1.234567,
    currency: "USDT",
    destination_masked: index === 0 ? "TAbc…Wwb" : `Tmasked${index}…end`,
    status: "APPROVED",
    risk_score: null,
    risk_flags: [],
    required_approvals: 2,
    approval_count: 2,
    proposal_digest: digest,
    provider: "mock",
  }));
  let queueCalls = 0;
  let releaseCalls = 0;
  let lastRelease = null;
  let enrollCalls = 0;

  await page.route("**/api/**", async route => {
    const req = route.request();
    const url = new URL(req.url());
    const pathname = url.pathname;
    if (pathname === "/api/state") {
      return json(route, { mode: "PAPER", equity: 1000, realized_pnl: 0, unrealized_pnl: 0, drawdown: 0, daily_loss: 0, live_enabled: false, kill_switch: false, open_positions: [] });
    }
    if (pathname === "/api/trades") return json(route, []);
    if (pathname === "/api/admin/withdrawals/summary") {
      return json(route, { pending_count: 0, pending_amount: 0, approved_count: 100, approved_amount: 121, in_flight_count: 0, rejected_count: 0 });
    }
    if (pathname === "/api/admin/withdrawals" && req.method() === "GET") {
      queueCalls += 1;
      return json(route, url.searchParams.get("status") === "APPROVED" ? rows : []);
    }
    if (pathname === "/api/admin/security/roles") return json(route, []);
    if (pathname === "/api/admin/billing/margin") return json(route, { gross_subscription_revenue: 0, operating_costs: 0, referral_commissions: 0, contribution_margin_pct: 0, contribution_profit: 0 });
    if (pathname === "/api/admin/auth/mfa/enroll" && req.method() === "POST") {
      enrollCalls += 1;
      return json(route, { totp: { secret: "TEST-ONLY-SECRET", qr_code: '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100"><rect width="100" height="100" fill="#fff"/><rect x="10" y="10" width="28" height="28" fill="#000"/><rect x="62" y="10" width="28" height="28" fill="#000"/><rect x="10" y="62" width="28" height="28" fill="#000"/><path d="M48 48h8v8h-8zM62 62h8v8h-8z" fill="#000"/></svg>' } });
    }
    if (pathname === "/api/admin/withdrawals/1/release" && req.method() === "POST") {
      releaseCalls += 1;
      lastRelease = { headers: req.headers(), body: req.postDataJSON() };
      return json(route, { ok: true, id: 1, status: "SUBMITTED", provider: "mock", reconcile_required: true });
    }
    return json(route, {});
  });

  const response = await page.goto(`${baseUrl}/admin`);
  assert.equal(response.status(), 200, "admin dashboard should load");
  const csp = response.headers()["content-security-policy"];
  assert.match(csp, /script-src 'self';/);
  assert.doesNotMatch(csp, /script-src[^;]*'unsafe-inline'/);
  await page.locator("#operatorIdentity").waitFor({ state: "visible" });
  assert.equal(await page.locator("#adminId").inputValue(), "admin-user-1", "admin ID should follow the JWT subject");
  assert.equal(await page.locator("#releaseId").inputValue(), "admin-user-1", "release operator should follow the authenticated identity");
  assert.equal(await page.locator("#adminId").getAttribute("readonly"), "");
  assert.equal(await page.locator("#releaseId").getAttribute("readonly"), "");

  await page.locator("#wdFilter").selectOption("APPROVED");
  await waitUntil(async () => (await page.locator("#withdrawalRows tr").count()) === 100);
  assert.equal(await page.locator("#withdrawals tbody#withdrawalRows").count(), 1, "the queue should stay nested in the table body");
  const firstRow = page.locator("#withdrawalRows tr").first();
  assert.match(await firstRow.locator("td").nth(2).innerText(), /12\.345678/, "withdrawal amount must preserve six decimal places");
  assert.match(await firstRow.locator("td").nth(4).innerText(), /n\/a/, "missing risk score must not appear as zero");
  assert.match(await firstRow.locator("td").nth(6).innerText(), new RegExp(digest), "proposal digest should appear next to Execute");

  await page.locator("#releaseToken").fill("test-release-token");
  let declinedConfirmation = "";
  page.once("dialog", async dialog => { declinedConfirmation = dialog.message(); await dialog.dismiss(); });
  await page.locator('[data-action="execute-withdrawal"][data-withdrawal-id="1"]').click();
  assert.match(declinedConfirmation, /12\.345678 USDT/);
  assert.match(declinedConfirmation, /TAbc…Wwb/);
  assert.match(declinedConfirmation, new RegExp(digest));
  assert.equal(releaseCalls, 0, "dismissing confirmation must never dispatch release");

  await page.locator("#releaseToken").fill("test-release-token");
  let acceptedConfirmation = "";
  page.once("dialog", async dialog => { acceptedConfirmation = dialog.message(); await dialog.accept(); });
  await page.locator('[data-action="execute-withdrawal"][data-withdrawal-id="1"]').click();
  await waitUntil(async () => releaseCalls === 1);
  assert.match(acceptedConfirmation, /Proposal SHA-256/);
  assert.equal(lastRelease.body.operator_id, "admin-user-1");
  assert.equal(lastRelease.headers["x-release-token"], "test-release-token", "the release secret must be scoped to the release action");

  assert.equal(await page.locator("#adminEnrollBtn").isHidden(), true, "an MFA-verified session must not start another enrollment flow");

  const startedQueueCalls = queueCalls;
  for (let i = 0; i < 12; i++) {
    const responsePromise = page.waitForResponse(response => response.url().includes("/api/admin/withdrawals?status=APPROVED") && response.request().method() === "GET");
    await page.getByRole("button", { name: "Refresh queue" }).click();
    await responsePromise;
    assert.equal(await page.locator("#withdrawalRows tr").count(), 100, `queue stress iteration ${i + 1} must render all rows`);
  }
  assert.ok(queueCalls >= startedQueueCalls + 12, "stress run should issue a queue request for every refresh");
  const duplicateIds = await page.locator("[id]").evaluateAll(elements => {
    const ids = elements.map(element => element.id).filter(Boolean);
    return ids.filter((id, index) => ids.indexOf(id) !== index);
  });
  assert.deepEqual(duplicateIds, [], "repeated refreshes must not create duplicate DOM IDs");
  await page.setViewportSize({ width: 390, height: 844 });
  const mobileLayout = await page.evaluate(() => ({ viewport: window.innerWidth, document: document.documentElement.scrollWidth, offenders: Array.from(document.querySelectorAll("body *")).map(el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return { tag: el.tagName, id: el.id, className: typeof el.className === "string" ? el.className : "", left: Math.round(r.left), right: Math.round(r.right), width: Math.round(r.width), position: s.position, insideTableScroller: !!el.closest(".table-wrap") }; }).filter(el => el.width > 0 && el.right > window.innerWidth + 2 && el.position !== "fixed" && !el.insideTableScroller).sort((a, b) => b.right - a.right).slice(0, 10) }));
  assert.ok(mobileLayout.document <= mobileLayout.viewport + 2, "admin dashboard must not force page-level horizontal scrolling on mobile: " + JSON.stringify(mobileLayout));
  const nav = page.locator(".mobile-nav");
  assert.equal(await nav.getAttribute("aria-label"), "Toggle navigation", "mobile nav button must have a meaningful accessible name");
  await nav.click();
  assert.equal(await page.locator("body").evaluate(body => body.classList.contains("nav-open")), true, "mobile nav toggle should open the navigation");
  await nav.click();
  const unnamedButtons = await page.locator("button:visible").evaluateAll(buttons => buttons.filter(button => !(button.getAttribute("aria-label") || button.getAttribute("title") || button.innerText.trim())).map(button => button.outerHTML));
  assert.deepEqual(unnamedButtons, [], "visible dashboard buttons must have an accessible name");
  assert.deepEqual(await page.evaluate(() => window.__cspViolations), [], "admin dashboard must have no CSP violations");
  assert.deepEqual(pageErrors, [], "admin browser flow must not throw JavaScript errors");
  await page.close();
}

async function testAdminFirstTimeMfaEnrollment(browser, baseUrl) {
  const page = await browser.newPage({ viewport: { width: 1365, height: 900 } });
  const pageErrors = [];
  page.on("pageerror", error => pageErrors.push(error.message));
  const beforeMfaToken = "e30." + Buffer.from(JSON.stringify({ sub: "admin-user-setup", aal: "aal1" })).toString("base64url") + ".test-signature";
  const upgradedToken = "e30." + Buffer.from(JSON.stringify({ sub: "admin-user-setup", aal: "aal2", amr: [{ method: "totp" }] })).toString("base64url") + ".test-signature";
  let enrollBody = null;
  let challengeBody = null;
  let verifyBody = null;
  let challengeCalls = 0;
  await page.route("**/api/**", async route => {
    const req = route.request();
    const url = new URL(req.url());
    const p = url.pathname;
    if (p === "/api/admin/auth/login" && req.method() === "POST") {
      return json(route, { ok: true, access_token: beforeMfaToken, refresh_token: "test-refresh", mfa_required: true, totp_enrolled: false });
    }
    if (p === "/api/admin/auth/mfa/enroll" && req.method() === "POST") {
      enrollBody = req.postDataJSON();
      return json(route, { id: "new-factor-id", totp: { secret: "TEST-ONLY-SECRET", qr_code: '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100"><rect width="100" height="100" fill="#fff"/><rect x="10" y="10" width="28" height="28" fill="#000"/><rect x="62" y="10" width="28" height="28" fill="#000"/><rect x="10" y="62" width="28" height="28" fill="#000"/></svg>' } });
    }
    if (p === "/api/admin/auth/mfa/challenge" && req.method() === "POST") {
      challengeCalls += 1;
      challengeBody = req.postDataJSON();
      return json(route, { id: "new-challenge-id" });
    }
    if (p === "/api/admin/auth/mfa/verify" && req.method() === "POST") {
      verifyBody = req.postDataJSON();
      return json(route, { access_token: upgradedToken, refresh_token: "upgraded-test-refresh", token_type: "bearer", expires_in: 300 });
    }
    if (p === "/api/state") return json(route, { mode: "PAPER", equity: 0, realized_pnl: 0, unrealized_pnl: 0, drawdown: 0, daily_loss: 0, live_enabled: false, kill_switch: false, open_positions: [] });
    if (p === "/api/trades") return json(route, []);
    if (p === "/api/admin/withdrawals/summary") return json(route, { pending_count: 0, pending_amount: 0, approved_count: 0, approved_amount: 0, in_flight_count: 0, rejected_count: 0 });
    if (p === "/api/admin/withdrawals" && req.method() === "GET") return json(route, []);
    if (p === "/api/admin/security/roles") return json(route, []);
    if (p === "/api/admin/billing/margin") return json(route, { gross_subscription_revenue: 0, operating_costs: 0, referral_commissions: 0, contribution_margin_pct: 0, contribution_profit: 0 });
    return json(route, {});
  });

  await page.goto(`${baseUrl}/admin`);
  await page.locator("#adminEmail").fill("setup-admin@example.invalid");
  await page.locator("#adminPassword").fill("mock-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await waitUntil(async () => (await page.locator("#authState").innerText()).includes("Enroll Google Authenticator"));
  assert.equal(await page.locator("#adminEnrollBtn").isHidden(), false, "an unenrolled admin must be offered enrollment without entering the protected polling state");

  const popupPromise = page.waitForEvent("popup");
  await page.getByRole("button", { name: "Enroll Google Authenticator" }).click();
  const popup = await popupPromise;
  await popup.locator("img[alt='Authenticator enrollment QR code']").waitFor();
  await waitUntil(async () => popup.locator("img").evaluate(img => img.complete && img.naturalWidth > 0));
  await waitUntil(async () => (await page.locator("#authState").innerText()).includes("six-digit authenticator code"));
  assert.equal(await page.locator("#adminMfaBox").isVisible(), true);
  assert.equal(await page.locator("#adminMfaVerifyBtn").isVisible(), true);
  assert.equal(enrollBody && Object.keys(enrollBody).length, 0, "enrollment request must not send arbitrary factor metadata from the browser");
  assert.equal(challengeCalls, 1);
  assert.deepEqual(challengeBody, { factor_id: "new-factor-id" });

  await page.locator("#adminMfaCode").fill("123456");
  await page.locator("#adminMfaVerifyBtn").click();
  await waitUntil(async () => (await page.locator("#authState").innerText()).includes("Administrator AAL2 session active"));
  assert.deepEqual(verifyBody, { factor_id: "new-factor-id", challenge_id: "new-challenge-id", code: "123456" });
  assert.equal(await page.evaluate(() => sessionStorage.getItem("ait_admin_mfa_verified")), "true");
  assert.equal(await page.evaluate(() => sessionStorage.getItem("ait_admin_access_token")), upgradedToken);
  assert.equal(await page.locator("#adminEnrollBtn").isHidden(), true, "enrollment must be hidden once MFA is verified");
  assert.deepEqual(pageErrors, [], "first-time admin enrollment flow must not throw browser JavaScript errors");
  await popup.close();
  await page.close();
}

async function testCustomerValidationError(browser, baseUrl) {
  const page = await browser.newPage();
  await page.addInitScript(() => {
    window.__cspViolations = [];
    window.addEventListener("securitypolicyviolation", event => window.__cspViolations.push(event.violatedDirective + ": " + event.blockedURI));
  });
  await page.route("**/api/**", async route => {
    const req = route.request();
    const url = new URL(req.url());
    if (url.pathname === "/api/auth/login") {
      return json(route, { detail: [
        { loc: ["body", "email"], msg: "field required", type: "missing" },
        { loc: ["body", "password"], msg: "field required", type: "missing" },
      ] }, 422);
    }
    return json(route, {});
  });
  await page.goto(`${baseUrl}/customer`);
  await page.locator("#toggle").click();
  await page.locator("#email").fill("customer@example.invalid");
  await page.locator("#password").fill("not-a-real-password");
  await page.locator("#submit").click();
  await waitUntil(async () => (await page.locator("#authOut").innerText()).includes("email: field required"));
  const output = await page.locator("#authOut").innerText();
  assert.match(output, /email: field required; password: field required/);
  assert.doesNotMatch(output, /\[object Object\]/);
  assert.deepEqual(await page.evaluate(() => window.__cspViolations), [], "customer validation page must run under strict script CSP without violations");
  await page.close();
}

async function testCustomerMfaOtpWithdrawalFlow(browser, baseUrl) {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  const pageErrors = [];
  await page.addInitScript(() => {
    window.__cspViolations = [];
    window.addEventListener("securitypolicyviolation", event => window.__cspViolations.push(event.violatedDirective + ": " + event.blockedURI));
  });
  page.on("pageerror", error => pageErrors.push(error.message));
  let sentIntent = null;
  let verifiedStepUp = null;
  let withdrawalRequest = null;
  let otpSendCount = 0;
  let otpVerifyCount = 0;
  let withdrawalPostCount = 0;
  await page.route("**/api/**", async route => {
    const req = route.request();
    const url = new URL(req.url());
    const p = url.pathname;
    if (p === "/api/auth/login" && req.method() === "POST") return json(route, { access_token: "customer-aal1-token" });
    if (p === "/api/auth/mfa/status") return json(route, { enabled: true, aal: "aal1", factor_count: 1, factor_ids: ["totp-factor"] });
    if (p === "/api/auth/mfa/challenge" && req.method() === "POST") return json(route, { id: "totp-challenge" });
    if (p === "/api/auth/mfa/verify" && req.method() === "POST") return json(route, { access_token: "customer-aal2-token" });
    if (p === "/api/auth/otp/send" && req.method() === "POST") {
      otpSendCount += 1;
      sentIntent = req.postDataJSON();
      return json(route, { otp_intent: "mock-otp-intent" });
    }
    if (p === "/api/auth/otp/verify" && req.method() === "POST") {
      otpVerifyCount += 1;
      const body = req.postDataJSON();
      assert.equal(body.otp_intent_token, "mock-otp-intent");
      sentIntent = { ...sentIntent, verify_body: body };
      verifiedStepUp = "mock-step-up-token";
      return json(route, { step_up_token: verifiedStepUp, expires_in: 600 });
    }
    if (p === "/api/customer/withdrawals" && req.method() === "POST") {
      withdrawalPostCount += 1;
      withdrawalRequest = { body: req.postDataJSON(), headers: req.headers() };
      return json(route, { ok: true, id: 777, request_id: "mock-customer-withdrawal", status: "PENDING", required_approvals: 2 });
    }
    if (p === "/api/customer/withdrawals" && req.method() === "GET") return json(route, []);
    if (p === "/api/customer/me") return json(route, { email: "customer@example.invalid", status: "ACTIVE", full_name: "Browser Test Customer" });
    if (p === "/api/customer/wallets") return json(route, [{ currency: "USDT", available_balance: 100, locked_balance: 0 }]);
    if (p === "/api/customer/portfolio") return json(route, { equity: 100, cash_equity: 100, positions: [] });
    if (p === "/api/customer/billing") return json(route, { plan: { name: "Free", code: "free" }, subscription: { status: "active", plan_code: "free" } });
    if (p === "/api/customer/ledger") return json(route, { entries: [] });
    if (p === "/api/customer/market-scanner") return json(route, { results: [] });
    if (p === "/api/customer/usdt/deposit") return json(route, { deposits: [], address: null });
    if (p === "/api/customer/strategy-lab/candidates") return json(route, []);
    if (p === "/api/customer/executors") return json(route, []);
    return json(route, {});
  });

  await page.goto(`${baseUrl}/customer`);
  await page.locator("#toggle").click();
  await page.locator("#email").fill("customer@example.invalid");
  await page.locator("#password").fill("mock-password");
  await page.locator("#submit").click();
  await waitUntil(async () => (await page.locator("#mfaOut").innerText()).includes("challenge ready"));
  await page.locator("#mfaCode").fill("123456");
  await page.getByRole("button", { name: "Verify authenticator" }).click();
  await page.locator("#account").waitFor({ state: "visible", timeout: 10000 });
  await page.locator('#sidebar a.nav-link[data-section="wallet"]').click();
  const mobileLayout = await page.evaluate(() => ({ viewport: window.innerWidth, document: document.documentElement.scrollWidth }));
  assert.ok(mobileLayout.document <= mobileLayout.viewport + 2, "customer dashboard must not force page-level horizontal scrolling on mobile: " + JSON.stringify(mobileLayout));
  await page.locator("#wdAmount").fill("12.345678");
  await page.locator("#wdDestination").fill("TAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA");
  await page.getByRole("button", { name: "Send OTP" }).click();
  await waitUntil(async () => (await page.locator("#otpOut").innerText()).includes("OTP sent"));
  await page.locator("#withdrawalOtp").fill("654321");
  await page.getByRole("button", { name: "Verify OTP" }).click();
  await waitUntil(async () => (await page.locator("#otpOut").innerText()).includes("Verification active"));
  let confirmation = "";
  page.once("dialog", async dialog => { confirmation = dialog.message(); await dialog.accept(); });
  await page.getByRole("button", { name: "Submit withdrawal request" }).click();
  await waitUntil(async () => withdrawalPostCount === 1 && (await page.locator("#wdOut").innerText()).includes("mock-customer-withdrawal"));
  assert.match(confirmation, /12\.345678 USDT/);
  assert.equal(otpSendCount, 1);
  assert.equal(otpVerifyCount, 1);
  assert.equal(sentIntent.amount, 12.345678);
  assert.equal(sentIntent.destination, "TAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA");
  assert.equal(sentIntent.network, "TRON");
  assert.equal(sentIntent.verify_body.amount, 12.345678);
  assert.equal(withdrawalRequest.body.amount, 12.345678);
  assert.equal(withdrawalRequest.body.network, "TRON");
  assert.equal(withdrawalRequest.headers["x-withdrawal-step-up"], verifiedStepUp, "withdrawal must use the one-time step-up token");
  assert.match(withdrawalRequest.headers["idempotency-key"] || "", /^[0-9a-f-]{36}$/i, "withdrawal POST must carry an idempotency key");
  assert.deepEqual(pageErrors, [], "mocked customer MFA/OTP/withdrawal flow must not throw JavaScript errors");
  assert.deepEqual(await page.evaluate(() => window.__cspViolations), [], "customer MFA/withdrawal flow must have no CSP violations under strict script policy");
  await page.close();
}

(async () => {
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  const baseUrl = `http://127.0.0.1:${address.port}`;
  const browser = await chromium.launch({ headless: true });
  try {
    await testAdminDashboard(browser, baseUrl);
    await testAdminFirstTimeMfaEnrollment(browser, baseUrl);
    await testCustomerValidationError(browser, baseUrl);
    await testCustomerMfaOtpWithdrawalFlow(browser, baseUrl);
    console.log("PASS: admin dashboard CSP, data-URI image, payout confirmation and 12x100-row refresh stress; customer validation and mocked login→MFA→OTP→withdrawal flow.");
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
