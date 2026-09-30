(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  // MAX_PROVIDERS (16) × MAX_TIMEOUT_SECONDS (45), with response overhead.
  const COMPLETION_TIMEOUT_MS = 735000;
  const loopback = ["localhost", "127.0.0.1", "[::1]", "::1"].includes(window.location.hostname)
    || window.location.hostname.endsWith(".localhost");
  const secureTransport = window.isSecureContext || (loopback && window.location.protocol === "http:");
  let completing = false;
  let planning = false;
  let refreshing = false;

  async function requestJSON(url, options = {}, timeoutMs = 10000) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, {
        ...options,
        cache: "no-store",
        credentials: "same-origin",
        redirect: "error",
        signal: controller.signal,
        headers: { "Accept": "application/json", ...(options.headers || {}) },
      });
      const rawJSON = await response.text();
      const payload = JSON.parse(rawJSON);
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new Error("Invalid JSON object");
      return { payload, rawJSON, status: response.status, ok: response.ok, receiptHeader: response.headers.get("X-SZL-Receipt") };
    } finally {
      window.clearTimeout(timer);
    }
  }

  function requireOK(result) {
    if (result.ok) return result.payload;
    const detail = result.payload && result.payload.detail;
    const code = detail && typeof detail.code === "string" ? ` · ${detail.code}` : "";
    const error = new Error(`HTTP ${result.status}${code}`);
    error.payload = result.payload;
    throw error;
  }

  function failureText(error) {
    return error.name === "AbortError"
      ? "Request timed out. Provider outcome is unknown; no answer is established."
      : error.payload ? error.message : "Network or response error. Evidence is unavailable.";
  }

  function element(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = String(value);
    return node;
  }

  function renderProviders(payload) {
    const providers = $("#providers");
    providers.replaceChildren();
    const rows = Array.isArray(payload.providers) ? payload.providers : [];
    $("#provider-count").textContent = rows.length;
    $("#registry-state").textContent = payload.state || "UNAVAILABLE";
    $("#egress-state").textContent = payload.egress_enabled ? "ENABLED" : "DISABLED";
    if (!rows.length) {
      providers.append(element("div", "provider", "No validated providers. Registry configuration and explicit egress admission are required for inference."));
    }
    for (const row of rows) {
      const card = element("article", "provider");
      card.setAttribute("role", "listitem");
      card.append(element("h3", "", row.id));
      card.append(element("p", "", `Models · ${(row.models || []).join(", ") || "none"}`));
      card.append(element("p", "", `Sovereignty ${row.sovereignty} · Priority ${row.priority} · Cost ${row.cost_tier}`));
      card.append(element("p", "", `Classifications · ${(row.classifications || []).join(", ")}`));
      card.append(element("p", "", row.enabled ? "Provider enabled" : "Provider disabled"));
      card.append(element("span", `state ${row.credential_state === "AVAILABLE" ? "available" : "unavailable"}`, row.credential_state || "UNAVAILABLE"));
      providers.append(card);
    }
  }

  function renderCandidates(payload) {
    const candidates = $("#candidates");
    candidates.className = "candidates";
    candidates.replaceChildren();
    const rows = Array.isArray(payload.candidates) ? payload.candidates : [];
    if (!rows.length) {
      candidates.classList.add("empty");
      candidates.textContent = "No enabled provider satisfies this model, classification, and cost policy.";
    }
    rows.forEach((row, index) => {
      const card = element("article", "candidate");
      const copy = element("span");
      copy.append(element("strong", "", row.provider_id));
      copy.append(element("small", "", `${row.public_model} → ${row.upstream_model} · credential ${row.credential_state}`));
      card.append(element("span", "rank", String(index + 1).padStart(2, "0")), copy);
      card.append(element("span", "score", `SOV ${row.sovereignty}\nPRI ${row.priority}\nCOST ${row.cost_tier}`));
      candidates.append(card);
    });
  }

  function policy() {
    if (!$("#plan-form").reportValidity()) return null;
    const model = $("#model").value.trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$/.test(model)) {
      $("#model").focus();
      return null;
    }
    const cost = Number($("#cost").value);
    if (!Number.isInteger(cost) || cost < 0 || cost > 10) return null;
    return { model, data_classification: $("#classification").value, max_cost_tier: cost };
  }

  function updatePolicy() {
    $("#completion-policy").textContent = `Next request: model ${$("#model").value || "UNSET"} · classification ${$("#classification").value} · maximum cost tier ${$("#cost").value}.`;
  }

  async function refreshRegistry() {
    if (refreshing) return;
    refreshing = true;
    $("#refresh").disabled = true;
    $("#runtime").textContent = "CONTROL PLANE · CHECKING";
    // Readiness can return 503 while registry and source evidence remain usable.
    const [registry, source, readiness] = await Promise.allSettled([
      requestJSON("/api/routes").then(requireOK),
      requestJSON("/api/source").then(requireOK),
      requestJSON("/readyz"),
    ]);
    if (registry.status === "fulfilled") {
      renderProviders(registry.value);
    } else {
      $("#providers").replaceChildren(element("div", "provider", "Registry evidence unavailable."));
      for (const id of ["registry-state", "provider-count", "egress-state"]) $("#" + id).textContent = "UNAVAILABLE";
    }
    if (source.status === "fulfilled") {
      $("#revision").textContent = `reported revision ${source.value.revision || "UNAVAILABLE"}`;
      $("#source-proof").textContent = JSON.stringify(source.value, null, 2);
    } else {
      $("#revision").textContent = "revision UNAVAILABLE";
      $("#source-proof").textContent = "Source evidence unavailable.";
    }
    if (readiness.status === "fulfilled") {
      const { payload, status, ok } = readiness.value;
      const controlReady = ok && payload.status === "ready";
      $("#runtime").textContent = controlReady ? "CONTROL PLANE · READY" : "CONTROL PLANE · NOT READY";
      $("#control-evidence").textContent = `${controlReady ? "READY" : "NOT READY"} · HTTP ${status} · CONTROL_PLANE`;
      const inference = payload.inference || {};
      $("#inference-state").textContent = `${inference.ready_for_requests === true ? "CONFIGURED" : "UNAVAILABLE"} · ${inference.basis || "LOCAL_CONFIGURATION_ONLY"}`;
      $("#inference-checks").textContent = Object.entries(inference.checks || {}).map(([name, passed]) => `${name}: ${passed === true ? "PASS" : "FAIL"}`).join(" · ");
    } else {
      $("#runtime").textContent = "CONTROL PLANE · UNAVAILABLE";
      $("#control-evidence").textContent = "Control readiness evidence unavailable.";
      $("#inference-state").textContent = "UNAVAILABLE · no current configuration evidence";
      $("#inference-checks").textContent = "";
    }
    refreshing = false;
    $("#refresh").disabled = false;
  }

  async function runPlan(event) {
    event.preventDefault();
    if (planning) return;
    const request = policy();
    if (!request) return;
    planning = true;
    $("#run-plan").disabled = true;
    $("#plan-state").textContent = "PLANNING";
    $("#receipt-short").textContent = "UNAVAILABLE";
    $("#output").textContent = "Computing deterministic policy order…";
    try {
      const payload = requireOK(await requestJSON("/api/plan", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(request),
      }));
      renderCandidates(payload);
      $("#output").textContent = JSON.stringify(payload, null, 2);
      $("#plan-state").textContent = payload.selected ? `FIRST RANKED · ${payload.selected}` : "NO POLICY CANDIDATE";
      $("#receipt-short").textContent = payload.receipt && typeof payload.receipt.digest === "string" ? `${payload.receipt.digest.slice(0, 12)}…` : "UNAVAILABLE";
    } catch (error) {
      $("#candidates").className = "candidates empty";
      $("#candidates").textContent = "Route planning failed closed.";
      $("#output").textContent = failureText(error);
      $("#plan-state").textContent = "BLOCKED";
    } finally {
      planning = false;
      $("#run-plan").disabled = false;
    }
  }

  function renderAttempts(receipt) {
    const attempts = $("#attempts");
    attempts.replaceChildren();
    const rows = receipt && Array.isArray(receipt.attempts) ? receipt.attempts : [];
    if (!rows.length) attempts.append(element("li", "", "No provider attempt evidence received."));
    rows.forEach((row) => attempts.append(element("li", "", `${row.provider_id} · ${row.state}${row.status_code ? ` · HTTP ${row.status_code}` : ""}`)));
  }

  function assistantText(completion) {
    return (Array.isArray(completion.choices) ? completion.choices : []).map((choice, index) => {
      const message = choice.message || {};
      const parts = [];
      if (typeof message.content === "string") parts.push(message.content);
      if (typeof message.refusal === "string") parts.push(`Refusal: ${message.refusal}`);
      for (const name of ["tool_calls", "function_call", "audio"]) {
        if (message[name] != null) parts.push(`${name}: ${JSON.stringify(message[name], null, 2)}`);
      }
      return `Choice ${index + 1}${choice.finish_reason ? ` · ${choice.finish_reason}` : ""}\n${parts.join("\n") || "No text output."}`;
    }).join("\n\n") || "No assistant output received.";
  }

  async function complete(event) {
    event.preventDefault();
    if (completing) return;
    if (!secureTransport) {
      $("#completion-state").textContent = "BLOCKED · SECURE TRANSPORT REQUIRED";
      $("#caller-token").value = "";
      return;
    }
    const request = policy();
    if (!request || !$("#completion-form").reportValidity()) return;
    const prompt = $("#prompt").value;
    const maxTokens = Number($("#max-tokens").value);
    const token = $("#caller-token").value;
    if (!prompt.trim() || prompt.length > 32000 || !token || token.length > 4096
      || !Number.isInteger(maxTokens) || maxTokens < 1 || maxTokens > 131072) {
      $("#completion-state").textContent = "INVALID REQUEST · CHECK PROMPT, TOKEN, AND OUTPUT LIMIT";
      return;
    }
    const sentRequest = { ...request, messages: [{ role: "user", content: prompt }], stream: false, max_tokens: maxTokens };
    completing = true;
    $("#send-completion").disabled = true;
    $("#caller-token").value = "";
    $("#caller-token").disabled = true;
    $("#completion-form").setAttribute("aria-busy", "true");
    $("#completion-state").textContent = `REQUESTING · ${request.model} · ${request.data_classification} · COST ≤ ${request.max_cost_tier}`;
    $("#answer-output").textContent = "Waiting for a bounded provider outcome…";
    $("#completion-receipt").textContent = "No receipt received for this request.";
    $("#verification-state").textContent = "UNAVAILABLE";
    $("#verify-output").textContent = "No consistency checks received for this request.";
    renderAttempts(null);
    try {
      const received = await requestJSON("/v1/chat/completions", {
        method: "POST", headers: { "Content-Type": "application/json", "Authorization": `Bearer ${token}` },
        body: JSON.stringify(sentRequest),
      }, COMPLETION_TIMEOUT_MS);
      const completion = requireOK(received);
      $("#answer-output").textContent = assistantText(completion);
      $("#completion-receipt").textContent = JSON.stringify(completion.szl_receipt || { state: "MISSING_RECEIPT" }, null, 2);
      renderAttempts(completion.szl_receipt);
      $("#completion-state").textContent = "ANSWER RECEIVED · VERIFYING";
      try {
        const verification = await requestJSON("/api/verify", {
          method: "POST", headers: {
            "Content-Type": "application/json",
            ...(received.receiptHeader ? { "X-SZL-Receipt": received.receiptHeader } : {}),
          },
          // Preserve Python integers and numeric forms beyond JavaScript precision.
          body: `{"completion":${received.rawJSON},"request":${JSON.stringify(sentRequest)}}`,
        });
        const checks = verification.payload.checks || {};
        const consistent = verification.ok && verification.payload.status === "CONSISTENT"
          && Boolean(received.receiptHeader) && checks.header_digest === true
          && ["receipt_digest", "response_digest", "request_digest"].every((name) => checks[name] === true);
        $("#verification-state").textContent = consistent ? "CONSISTENT · UNSIGNED_HONEST" : "DIVERGENT / UNVERIFIED";
        $("#verify-output").textContent = JSON.stringify(verification.payload, null, 2);
        $("#completion-state").textContent = consistent ? "ANSWER RECEIVED · CONTENT CONSISTENT" : "ANSWER RECEIVED · VERIFICATION FAILED";
      } catch (error) {
        $("#verification-state").textContent = "UNAVAILABLE · UNSIGNED_HONEST";
        $("#verify-output").textContent = failureText(error);
        $("#completion-state").textContent = "ANSWER RECEIVED · UNVERIFIED";
      }
    } catch (error) {
      $("#completion-state").textContent = "REQUEST FAILED · NO ANSWER ESTABLISHED";
      $("#answer-output").textContent = failureText(error);
      const detail = error.payload && error.payload.detail;
      $("#completion-receipt").textContent = detail ? JSON.stringify(detail, null, 2) : "No failure receipt received. Provider outcome is unknown.";
      renderAttempts(detail);
    } finally {
      completing = false;
      $("#send-completion").disabled = false;
      $("#caller-token").disabled = false;
      $("#completion-form").setAttribute("aria-busy", "false");
    }
  }

  $("#plan-form").addEventListener("submit", runPlan);
  $("#run-plan").addEventListener("click", () => $("#plan-form").requestSubmit());
  $("#refresh").addEventListener("click", refreshRegistry);
  $("#completion-form").addEventListener("submit", complete);
  for (const id of ["model", "classification", "cost"]) $("#" + id).addEventListener("input", updatePolicy);
  if (!secureTransport) {
    $("#caller-token").disabled = true;
    $("#send-completion").disabled = true;
    $("#token-note").textContent = "Caller credentials require a secure browser context or a loopback local page. Open the deployed router over HTTPS before sending a token.";
    $("#completion-state").textContent = "BLOCKED · SECURE TRANSPORT REQUIRED";
  }
  updatePolicy();
  refreshRegistry();
})();
