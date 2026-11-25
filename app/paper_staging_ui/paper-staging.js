(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    accessToken: "",
    factorId: "",
    challengeId: "",
    profile: null,
    initialized: false,
    sessionVersion: 0,
  };

  function setNotice(id, message, kind) {
    const node = $(id);
    if (!node) return;
    node.textContent = String(message || "");
    node.className = "notice" + (kind ? " " + kind : "");
  }

  function setBusy(button, busy, label) {
    if (!button) return;
    if (busy) {
      button.dataset.originalLabel = button.textContent;
      button.disabled = true;
      button.textContent = label || "Working…";
    } else {
      button.disabled = false;
      if (button.dataset.originalLabel) {
        button.textContent = button.dataset.originalLabel;
        delete button.dataset.originalLabel;
      }
    }
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[char]);
  }

  function numberText(value, digits) {
    if (value == null || value === "") return "—";
    const n = Number(value);
    if (!Number.isFinite(n)) return "—";
    return n.toLocaleString(undefined, { maximumFractionDigits: digits == null ? 6 : digits });
  }

  async function api(path, options) {
    const opts = Object.assign({ method: "GET", headers: {}, cache: "no-store" }, options || {});
    opts.headers = Object.assign({ "Accept": "application/json" }, opts.headers || {});
    if (state.accessToken) opts.headers.Authorization = "Bearer " + state.accessToken;
    if (opts.body && typeof opts.body !== "string") {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    const response = await fetch(path, opts);
    const contentType = response.headers.get("content-type") || "";
    let payload = null;
    if (contentType.includes("application/json")) {
      payload = await response.json();
    } else {
      payload = await response.text();
    }
    if (!response.ok) {
      const detail = payload && typeof payload === "object"
        ? (payload.detail || payload.message || payload.reason)
        : payload;
      const error = new Error(String(detail || ("Request failed (" + response.status + ")")));
      error.status = response.status;
      error.paperGate = response.headers.get("x-atlas-paper-gate") === "blocked";
      throw error;
    }
    return payload;
  }

  function showAuthState(label, signedIn) {
    $("auth-state").textContent = label;
    $("signout-button").disabled = !signedIn;
  }

  function showWorkspace(visible) {
    $("workspace").hidden = !visible;
    $("blocked-state").hidden = true;
  }

  function handlePaperGateError(error) {
    if (error && error.paperGate === true) {
      $("workspace").hidden = true;
      $("blocked-state").hidden = false;
      setNotice(
        "auth-message",
        "Paper access is blocked by staging readiness or risk controls: " +
          String(error.message || "The server is not ready."),
        "error"
      );
      return true;
    }
    if (error && (error.status === 401 || error.status === 403)) {
      signOut("Your authenticated paper session expired or no longer meets AAL2. Sign in again.");
      return true;
    }
    return false;
  }

  function revokeSessionToken(token) {
    const value = String(token || "").trim();
    if (!value) return;
    void fetch("/api/auth/logout", {
      method: "POST",
      headers: {
        "Accept": "application/json",
        "Authorization": "Bearer " + value
      },
      cache: "no-store",
      keepalive: true
    }).catch(() => {
      // Local credentials are cleared immediately. The server-side expiry still
      // protects requests if an Auth service/network failure prevents revocation.
    });
  }

  function signOut(message) {
    const tokenToRevoke = state.accessToken;
    // Invalidate every in-flight login/MFA continuation before clearing the token.
    // A late MFA response must never be able to restore an authenticated workspace.
    state.sessionVersion += 1;
    state.accessToken = "";
    state.factorId = "";
    state.challengeId = "";
    state.profile = null;
    state.initialized = false;

    $("email").value = "";
    $("password").value = "";
    $("totp-code").value = "";
    $("totp-secret").textContent = "—";
    $("totp-qr").removeAttribute("src");
    $("totp-qr").hidden = true;
    $("totp-setup").hidden = true;
    $("mfa-instructions").textContent = "Complete authenticator verification before paper access is enabled.";
    $("mfa-state").textContent = "Required";
    $("profile-summary").textContent = "";
    $("welcome").textContent = "Paper account";
    $("positions").innerHTML = '<p class="empty">No open paper positions.</p>';
    $("orders").innerHTML = '<p class="empty">No open paper orders.</p>';
    $("history").innerHTML = '<p class="empty">No order history loaded.</p>';
    $("scanner-results").innerHTML = '<p class="empty">No market scan loaded.</p>';
    $("cash-balance").textContent = "—";
    $("equity").textContent = "—";
    $("valuation").textContent = "Waiting for account setup";
    $("position-count").textContent = "—";
    $("order-count").textContent = "—";
    $("initialize-button").disabled = false;
    $("initialize-button").textContent = "Initialize virtual paper balance";
    $("mfa-panel").hidden = true;
    showAuthState("Not signed in", false);
    showWorkspace(false);
    setNotice("mfa-message", "Authenticator verification is required.", "info");
    setNotice("scanner-message", "Sign in to run a read-only market scan.", "info");
    setNotice("order-message", "Initialize the virtual account before submitting an order.", "info");
    setNotice("auth-message", message || "Signed out. Credentials have been cleared and the Auth session is being revoked.", "info");

    // Revoke the Supabase session server-side as well as dropping the in-memory
    // bearer token. The version guard above prevents any pending response from
    // writing a new token back into this page after sign-out.
    revokeSessionToken(tokenToRevoke);
  }

  function sessionIsCurrent(version) {
    return version === state.sessionVersion && Boolean(state.accessToken);
  }

  async function beginMfa(version = state.sessionVersion) {
    const status = await api("/api/auth/mfa/status");
    if (!sessionIsCurrent(version)) return;
    $("mfa-panel").hidden = false;
    $("mfa-state").textContent = status.aal === "aal2" ? "Verified" : "Required";

    if (status.aal === "aal2") {
      await finishCustomerAccess(version);
      return;
    }

    const ids = Array.isArray(status.factor_ids) ? status.factor_ids : [];
    if (status.enabled && ids.length) {
      state.factorId = String(ids[0]);
      $("enroll-button").hidden = true;
      $("verify-button").disabled = false;
      $("mfa-instructions").textContent = "Enter the current six-digit code from your enrolled authenticator.";
      await createMfaChallenge(version);
      if (!sessionIsCurrent(version)) return;
      setNotice("mfa-message", "Authenticator challenge created. Enter your current code.", "info");
      return;
    }

    state.factorId = "";
    state.challengeId = "";
    $("totp-setup").hidden = true;
    $("enroll-button").hidden = false;
    $("verify-button").disabled = true;
    $("mfa-instructions").textContent = "Set up a TOTP authenticator before paper trading. Google Authenticator is supported.";
    setNotice("mfa-message", "No verified authenticator was found. Set one up to continue.", "info");
  }

  async function createMfaChallenge(version = state.sessionVersion) {
    if (!state.factorId) throw new Error("No authenticator factor is selected.");
    const challenge = await api("/api/auth/mfa/challenge", {
      method: "POST",
      body: { factor_id: state.factorId }
    });
    if (!sessionIsCurrent(version)) return false;
    state.challengeId = String(challenge.id || challenge.challenge_id || "");
    if (!state.challengeId) throw new Error("The authenticator challenge response did not include an ID.");
    return true;
  }

  async function enrollMfa() {
    const version = state.sessionVersion;
    const button = $("enroll-button");
    setBusy(button, true, "Setting up…");
    try {
      const result = await api("/api/auth/mfa/enroll", { method: "POST", body: {} });
      if (!sessionIsCurrent(version)) return;
      if (result.enabled) {
        const ids = Array.isArray(result.factor_ids) ? result.factor_ids : [];
        if (!ids.length) throw new Error("An authenticator is already enrolled but its factor ID was not returned. Sign out and sign in again.");
        state.factorId = String(ids[0]);
        $("totp-setup").hidden = true;
      } else {
        state.factorId = String(result.id || "");
        const totp = result.totp || {};
        $("totp-secret").textContent = String(totp.secret || "Supabase did not return a manual setup key.");
        const qr = String(totp.qr_code || "");
        const image = $("totp-qr");
        if (qr) {
          image.src = qr.startsWith("data:image/")
            ? qr
            : "data:image/svg+xml;charset=utf-8," + encodeURIComponent(qr);
          image.hidden = false;
        } else {
          image.hidden = true;
        }
        $("totp-setup").hidden = false;
      }
      if (!state.factorId) throw new Error("The authenticator setup response did not include a factor ID.");
      const challengeCreated = await createMfaChallenge(version);
      if (!challengeCreated || !sessionIsCurrent(version)) return;
      $("verify-button").disabled = false;
      $("mfa-instructions").textContent = "Enter the six-digit code shown in your authenticator to confirm setup.";
      setNotice("mfa-message", "Authenticator setup is ready. Verify the code to finish.", "success");
    } catch (error) {
      if (sessionIsCurrent(version)) setNotice("mfa-message", error.message, "error");
    } finally {
      setBusy(button, false);
    }
  }

  async function verifyMfa() {
    const code = $("totp-code").value.trim();
    if (!/^\d{6}$/.test(code)) {
      setNotice("mfa-message", "Enter the six-digit authenticator code.", "error");
      return;
    }
    const version = state.sessionVersion;
    const button = $("verify-button");
    setBusy(button, true, "Verifying…");
    try {
      if (!state.challengeId) {
        const challengeCreated = await createMfaChallenge(version);
        if (!challengeCreated || !sessionIsCurrent(version)) return;
      }
      const result = await api("/api/auth/mfa/verify", {
        method: "POST",
        body: { factor_id: state.factorId, challenge_id: state.challengeId, code }
      });
      if (!sessionIsCurrent(version)) {
        // A late MFA response may carry a fresh access token after the user has
        // already signed out. Revoke it rather than allowing a stale promise to
        // restore the customer session.
        revokeSessionToken(result.access_token);
        return;
      }
      if (result.access_token) state.accessToken = String(result.access_token);
      state.challengeId = "";
      $("totp-code").value = "";
      const status = await api("/api/auth/mfa/status");
      if (!sessionIsCurrent(version)) return;
      if (status.aal !== "aal2") throw new Error("Authenticator code was accepted but the session is not AAL2. Sign in again and retry.");
      setNotice("mfa-message", "Authenticator verified. Loading your paper account…", "success");
      await finishCustomerAccess(version);
    } catch (error) {
      if (sessionIsCurrent(version)) {
        state.challengeId = "";
        setNotice("mfa-message", error.message, "error");
        if (state.factorId) {
          try { await createMfaChallenge(version); } catch (_) {}
        }
      }
    } finally {
      setBusy(button, false);
    }
  }

  async function finishCustomerAccess(version = state.sessionVersion) {
    try {
      await api("/api/auth/profile/provision", { method: "POST", body: {} });
      if (!sessionIsCurrent(version)) return;
      const profile = await api("/api/customer/me");
      if (!sessionIsCurrent(version)) return;
      state.profile = profile;
      $("welcome").textContent = "Welcome" + (profile.display_name ? ", " + profile.display_name : "");
      $("profile-summary").textContent = String(profile.email || "Authenticated test customer") +
        (profile.identity_review_required ? " · identity review required" : "");
      showAuthState("AAL2 verified", true);
      $("mfa-state").textContent = "Verified";
      $("mfa-panel").hidden = true;
      showWorkspace(true);
      setNotice("auth-message", "Signed in securely. Paper actions require the server-side staging checks to remain clear.", "success");
      await refreshData();
    } catch (error) {
      if (sessionIsCurrent(version)) {
        showWorkspace(false);
        setNotice("auth-message", error.message, "error");
      }
    }
  }

  async function login() {
    const button = $("login-button");
    const version = state.sessionVersion;
    const email = $("email").value.trim();
    const password = $("password").value;
    if (!email || !password) {
      setNotice("auth-message", "Enter your test-account email and password.", "error");
      return;
    }
    setBusy(button, true, "Signing in…");
    try {
      const result = await api("/api/auth/login", {
        method: "POST",
        body: { email, password }
      });
      if (version !== state.sessionVersion) {
        revokeSessionToken(result.access_token);
        return;
      }
      if (!result.access_token) {
        throw new Error("Supabase did not return a session. Confirm the test-account email, then sign in again.");
      }
      state.accessToken = String(result.access_token);
      showAuthState("First factor accepted", true);
      await beginMfa(version);
    } catch (error) {
      if (version === state.sessionVersion) signOut(error.message);
    } finally {
      setBusy(button, false);
    }
  }

  async function signup() {
    const button = $("signup-button");
    const version = state.sessionVersion;
    const email = $("email").value.trim();
    const password = $("password").value;
    if (!email || password.length < 8) {
      setNotice("auth-message", "Enter a valid email and a password with at least eight characters.", "error");
      return;
    }
    setBusy(button, true, "Creating…");
    try {
      const result = await api("/api/auth/signup", {
        method: "POST",
        body: { email, password }
      });
      if (version !== state.sessionVersion) {
        revokeSessionToken(result.access_token);
        return;
      }
      if (result.access_token) {
        state.accessToken = String(result.access_token);
        showAuthState("First factor accepted", true);
        await beginMfa(version);
      } else {
        setNotice("auth-message", "Account request accepted. Confirm the email from Supabase Auth, then return here and sign in.", "success");
      }
    } catch (error) {
      if (version === state.sessionVersion) setNotice("auth-message", error.message, "error");
    } finally {
      setBusy(button, false);
    }
  }

  async function refreshData() {
    if (!state.accessToken) return;
    const version = state.sessionVersion;
    const button = $("refresh-button");
    setBusy(button, true, "Refreshing…");
    try {
      const results = await Promise.all([
        api("/api/customer/portfolio"),
        api("/api/customer/paper/balances"),
        api("/api/customer/paper/positions"),
        api("/api/customer/paper/orders"),
        api("/api/customer/paper/history?limit=50")
      ]);
      // A previous customer's slow refresh must not repaint the page after sign-out
      // or after a different session has signed in.
      if (!sessionIsCurrent(version)) return;
      const portfolio = results[0] || {};
      const balances = Array.isArray(results[1].balances) ? results[1].balances : [];
      const positions = Array.isArray(results[2].positions) ? results[2].positions : [];
      const orders = Array.isArray(results[3].orders) ? results[3].orders : [];
      const history = Array.isArray(results[4].orders) ? results[4].orders : [];
      const cash = balances
        .filter((row) => String(row.account_code || "") === "PAPER_CASH" && String(row.currency || "").toUpperCase() === "USDT")
        .reduce((sum, row) => sum + Number(row.balance || 0), 0);
      state.initialized = balances.some((row) => String(row.account_code || "") === "PAPER_CASH");
      $("cash-balance").textContent = numberText(cash, 6) + " USDT";
      $("equity").textContent = portfolio.equity == null ? "Not valued" : numberText(portfolio.equity, 6) + " USDT";
      $("valuation").textContent = String(portfolio.valuation_status || "—").replaceAll("_", " ").toLowerCase();
      $("position-count").textContent = String(positions.length);
      $("order-count").textContent = String(orders.length);
      $("initialize-button").disabled = state.initialized;
      $("initialize-button").textContent = state.initialized ? "Virtual account initialized" : "Initialize virtual paper balance";
      renderPositions(positions);
      renderOrders(orders);
      renderHistory(history);
      showWorkspace(true);
      setNotice("order-message", state.initialized ? "Paper account is initialized. Orders remain virtual and are filled only by verified completed bars." : "Initialize the virtual account before submitting an order.", "info");
    } catch (error) {
      if (sessionIsCurrent(version) && !handlePaperGateError(error)) {
        setNotice("order-message", error.message, "error");
      }
    } finally {
      if (sessionIsCurrent(version)) setBusy(button, false);
    }
  }

  function renderPositions(positions) {
    const root = $("positions");
    if (!positions.length) {
      root.innerHTML = '<p class="empty">No open paper positions.</p>';
      return;
    }
    root.innerHTML = '<table><thead><tr><th>Market</th><th>Quantity</th><th>Average entry</th><th>Cost basis</th><th>Realized P&amp;L</th><th>Timeframe</th></tr></thead><tbody>' +
      positions.map((row) => '<tr><td>' + escapeHtml(row.symbol || "—") +
        '</td><td>' + escapeHtml(numberText(row.quantity, 8)) +
        '</td><td>' + escapeHtml(numberText(row.average_entry_price, 6)) +
        '</td><td>' + escapeHtml(numberText(row.cost_basis, 6)) +
        '</td><td>' + escapeHtml(numberText(row.realized_pnl, 6)) +
        '</td><td>' + escapeHtml(row.timeframe || "—") +
        '</td></tr>').join("") + '</tbody></table>';
  }

  function renderOrders(orders) {
    const root = $("orders");
    if (!orders.length) {
      root.innerHTML = '<p class="empty">No open paper orders.</p>';
      return;
    }
    root.innerHTML = '<table><thead><tr><th>Market</th><th>Side</th><th>Filled</th><th>Requested</th><th>Status</th><th>Timeframe</th><th>Action</th></tr></thead><tbody>' +
      orders.map((row) => {
        const id = String(row.client_order_id || "");
        const timeframe = row.metadata && row.metadata.timeframe ? row.metadata.timeframe : "—";
        return '<tr><td>' + escapeHtml(row.symbol || "—") +
          '</td><td>' + escapeHtml(row.side || "—") +
          '</td><td>' + escapeHtml(numberText(row.filled_quantity, 8)) +
          '</td><td>' + escapeHtml(numberText(row.quantity, 8)) +
          '</td><td>' + escapeHtml(row.status || "—") +
          '</td><td>' + escapeHtml(timeframe) +
          '</td><td><button class="mini" type="button" data-advance="' + escapeHtml(id) + '">Advance</button> ' +
          '<button class="mini muted-button" type="button" data-cancel="' + escapeHtml(id) + '">Cancel</button></td></tr>';
      }).join("") + '</tbody></table>';
  }

  function renderHistory(orders) {
    const root = $("history");
    if (!orders.length) {
      root.innerHTML = '<p class="empty">No paper orders have been submitted yet.</p>';
      return;
    }
    root.innerHTML = '<table><thead><tr><th>Created</th><th>Market</th><th>Side</th><th>Status</th><th>Filled / requested</th><th>Average fill</th><th>Fills</th></tr></thead><tbody>' +
      orders.map((row) => {
        const timestamp = row.created_at ? new Date(row.created_at) : null;
        const created = timestamp && Number.isFinite(timestamp.getTime())
          ? timestamp.toLocaleString()
          : "—";
        return '<tr><td>' + escapeHtml(created) +
          '</td><td>' + escapeHtml(row.symbol || "—") +
          '</td><td>' + escapeHtml(row.side || "—") +
          '</td><td>' + escapeHtml(row.status || "—") +
          '</td><td>' + escapeHtml(numberText(row.filled_quantity, 8)) + " / " +
          escapeHtml(numberText(row.quantity, 8)) +
          '</td><td>' + escapeHtml(numberText(row.average_fill_price, 8)) +
          '</td><td>' + escapeHtml(row.fill_count == null ? 0 : row.fill_count) +
          '</td></tr>';
      }).join("") + '</tbody></table>';
  }

  function renderScannerResults(results) {
    const root = $("scanner-results");
    if (!results.length) {
      root.innerHTML = '<p class="empty">The scanner returned no market rows.</p>';
      return;
    }
    root.innerHTML = '<table><thead><tr><th>Market</th><th>Signal</th><th>Score</th><th>Last price</th><th>RSI</th><th>ATR</th><th>Volume ratio</th></tr></thead><tbody>' +
      results.map((row) => {
        const score = Number(row.score);
        const scoreText = Number.isFinite(score) ? numberText(score * 100, 0) + "%" : "—";
        return '<tr><td>' + escapeHtml(row.symbol || "—") +
          '</td><td>' + escapeHtml(row.signal || "—") +
          '</td><td>' + escapeHtml(scoreText) +
          '</td><td>' + escapeHtml(numberText(row.price, 8)) +
          '</td><td>' + escapeHtml(numberText(row.rsi, 2)) +
          '</td><td>' + escapeHtml(numberText(row.atr, 8)) +
          '</td><td>' + escapeHtml(numberText(row.volume_ratio, 2)) +
          '</td></tr>';
      }).join("") + '</tbody></table>';
  }

  async function runMarketScanner() {
    const button = $("scanner-button");
    if (!state.accessToken) {
      setNotice("scanner-message", "Sign in and complete authenticator verification before scanning markets.", "error");
      return;
    }
    const version = state.sessionVersion;
    const requested = $("scanner-symbols").value
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean);
    const unique = [...new Set(requested)];
    if (!unique.length || unique.length > 8 || unique.some((symbol) => symbol.length > 40 || /[\u0000-\u001f\u007f]/.test(symbol))) {
      setNotice("scanner-message", "Enter between one and eight comma-separated market symbols (40 characters maximum each).", "error");
      return;
    }
    setBusy(button, true, "Scanning…");
    try {
      const query = new URLSearchParams({
        exchange: "binance",
        symbols: unique.join(",")
      });
      const result = await api("/api/customer/market-scanner?" + query.toString());
      if (!sessionIsCurrent(version)) return;
      const rows = Array.isArray(result.results) ? result.results : [];
      renderScannerResults(rows);
      const errors = rows.filter((row) => row.signal === "ERROR").length;
      setNotice(
        "scanner-message",
        "Read-only " + String(result.exchange || "binance") + " scan completed on " +
          String(result.timeframe || "15m") + " bars: " + rows.length + " market(s), " +
          errors + " unavailable. Indicators are informational only.",
        errors ? "info" : "success"
      );
    } catch (error) {
      if (sessionIsCurrent(version) && !handlePaperGateError(error)) {
        setNotice("scanner-message", error.message, "error");
      }
    } finally {
      if (sessionIsCurrent(version)) setBusy(button, false);
    }
  }

  async function initializePaperAccount() {
    const button = $("initialize-button");
    setBusy(button, true, "Initializing…");
    try {
      const result = await api("/api/customer/paper/account/initialize", { method: "POST", body: {} });
      setNotice("order-message", (result.status || "INITIALIZED") + ": " +
        String(result.note || "Virtual paper balance created.") + " Starting cash: " +
        numberText(result.starting_cash, 6) + " " + String(result.currency || "USDT") + ".", "success");
      await refreshData();
    } catch (error) {
      if (!handlePaperGateError(error)) {
        setNotice("order-message", error.message, "error");
      }
    } finally {
      setBusy(button, false);
      button.disabled = state.initialized;
    }
  }

  async function submitOrder(event) {
    event.preventDefault();
    if (!state.initialized) {
      setNotice("order-message", "Initialize the virtual paper account first.", "error");
      return;
    }
    const orderType = $("order-type").value;
    const quantity = Number($("quantity").value);
    const limitPrice = Number($("limit-price").value);
    if (!Number.isFinite(quantity) || quantity <= 0) {
      setNotice("order-message", "Enter a valid quantity greater than zero.", "error");
      return;
    }
    if (orderType === "LIMIT" && (!Number.isFinite(limitPrice) || limitPrice <= 0)) {
      setNotice("order-message", "Enter a valid positive limit price.", "error");
      return;
    }
    const payload = {
      client_order_id: (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : ("atlas-paper-" + Date.now() + "-" + Math.random().toString(16).slice(2)),
      symbol: $("symbol").value,
      side: $("side").value,
      quantity: String(quantity),
      order_type: orderType,
      timeframe: $("timeframe").value
    };
    if (orderType === "LIMIT") payload.limit_price = String(limitPrice);
    const button = $("submit-order");
    setBusy(button, true, "Submitting…");
    try {
      const result = await api("/api/customer/paper/orders", { method: "POST", body: payload });
      const status = result.status || (result.order && result.order.status) || "SUBMITTED";
      setNotice("order-message", "Paper order " + status + ". " +
        String(result.fill_note || "Advance only against a later verified completed bar."), "success");
      $("quantity").value = "";
      if (orderType === "LIMIT") $("limit-price").value = "";
      await refreshData();
    } catch (error) {
      if (!handlePaperGateError(error)) {
        setNotice("order-message", error.message, "error");
      }
    } finally {
      setBusy(button, false);
    }
  }

  async function advanceOrder(id) {
    if (!id) return;
    try {
      const result = await api("/api/customer/paper/orders/" + encodeURIComponent(id) + "/advance", { method: "POST", body: {} });
      setNotice("order-message", "Paper order advancement: " + String(result.status || "processed") + ". No exchange order was placed.", "success");
      await refreshData();
    } catch (error) {
      if (!handlePaperGateError(error)) {
        setNotice("order-message", error.message, "error");
      }
    }
  }

  async function cancelOrder(id) {
    if (!id || !window.confirm("Cancel this open virtual paper order?")) return;
    try {
      await api("/api/customer/paper/orders/" + encodeURIComponent(id) + "/cancel", {
        method: "POST",
        body: { idempotency_key: "paper-ui-cancel-" + ((window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now())) }
      });
      setNotice("order-message", "Paper order cancellation submitted.", "success");
      await refreshData();
    } catch (error) {
      if (!handlePaperGateError(error)) {
        setNotice("order-message", error.message, "error");
      }
    }
  }

  $("login-button").addEventListener("click", login);
  $("signup-button").addEventListener("click", signup);
  $("signout-button").addEventListener("click", () => signOut());
  $("enroll-button").addEventListener("click", enrollMfa);
  $("verify-button").addEventListener("click", verifyMfa);
  $("refresh-button").addEventListener("click", refreshData);
  $("retry-preflight").addEventListener("click", async () => {
    const button = $("retry-preflight");
    setBusy(button, true, "Checking…");
    try { await refreshData(); } finally { setBusy(button, false); }
  });
  $("initialize-button").addEventListener("click", initializePaperAccount);
  $("scanner-button").addEventListener("click", runMarketScanner);
  $("order-form").addEventListener("submit", submitOrder);
  $("order-type").addEventListener("change", () => {
    $("limit-field").hidden = $("order-type").value !== "LIMIT";
  });
  $("orders").addEventListener("click", (event) => {
    const advance = event.target.closest("[data-advance]");
    const cancel = event.target.closest("[data-cancel]");
    if (advance) advanceOrder(advance.dataset.advance);
    if (cancel) cancelOrder(cancel.dataset.cancel);
  });
  $("auth-form").addEventListener("submit", (event) => event.preventDefault());
  showWorkspace(false);
  showAuthState("Not signed in", false);
})();