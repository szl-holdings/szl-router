(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const runtime = $("#runtime");
  const providers = $("#providers");
  const candidates = $("#candidates");
  const output = $("#output");

  async function requestJSON(url, options = {}) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(url, {
        ...options,
        cache: "no-store",
        credentials: "same-origin",
        signal: controller.signal,
        headers: {
          "Accept": "application/json",
          ...(options.headers || {}),
        },
      });
      const payload = await response.json().catch(() => ({ detail: "Non-JSON response" }));
      if (!response.ok) {
        const detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail || payload);
        throw new Error(detail || `HTTP ${response.status}`);
      }
      return payload;
    } finally {
      window.clearTimeout(timer);
    }
  }

  function element(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = String(value);
    return node;
  }

  function credentialClass(value) {
    const normalized = String(value || "").toLowerCase();
    if (normalized === "available") return "available";
    if (normalized.includes("invalid")) return "invalid";
    return "unavailable";
  }

  function renderProviders(payload) {
    providers.replaceChildren();
    const rows = Array.isArray(payload.providers) ? payload.providers : [];
    $("#provider-count").textContent = rows.length;
    $("#registry-state").textContent = payload.state || "UNAVAILABLE";
    $("#egress-state").textContent = payload.egress_enabled ? "ENABLED" : "DISABLED";

    if (!rows.length) {
      const empty = element("div", "provider");
      empty.append(element("h3", "", "No validated providers"));
      empty.append(element("p", "", "The router remains useful as a deterministic planner contract, but egress stays disabled until an exact allowlisted registry is supplied."));
      providers.append(empty);
      return;
    }

    for (const row of rows) {
      const card = element("article", "provider");
      card.setAttribute("role", "listitem");
      card.append(element("h3", "", row.id));
      card.append(element("p", "", `Models · ${(row.models || []).join(", ") || "none"}`));
      card.append(element("p", "", `Sovereignty ${row.sovereignty} · Priority ${row.priority} · Cost ${row.cost_tier}`));
      card.append(element("p", "", `Classifications · ${(row.classifications || []).join(", ")}`));
      card.append(element("span", `state ${credentialClass(row.credential_state)}`, row.credential_state || "UNAVAILABLE"));
      providers.append(card);
    }
  }

  function renderCandidates(payload) {
    candidates.classList.remove("empty");
    candidates.replaceChildren();
    const rows = Array.isArray(payload.candidates) ? payload.candidates : [];
    if (!rows.length) {
      candidates.classList.add("empty");
      candidates.textContent = "No provider satisfies this model, classification, cost, credential, and registry policy.";
      return;
    }

    rows.forEach((row, index) => {
      const card = element("article", "candidate");
      const rank = element("span", "rank", String(index + 1).padStart(2, "0"));
      const copy = element("span");
      copy.append(element("strong", "", row.provider_id));
      copy.append(element("small", "", `${row.public_model} → ${row.upstream_model} · ${row.credential_state}`));
      const score = element("span", "score");
      score.textContent = `SOV ${row.sovereignty}\nPRI ${row.priority}\nCOST ${row.cost_tier}`;
      card.append(rank, copy, score);
      candidates.append(card);
    });
  }

  async function refreshRegistry() {
    runtime.textContent = "CONTROL PLANE · CHECKING";
    try {
      const [registry, source, readiness] = await Promise.all([
        requestJSON("/api/routes"),
        requestJSON("/api/source"),
        requestJSON("/readyz"),
      ]);
      renderProviders(registry);
      $("#revision").textContent = `revision ${source.revision || "UNAVAILABLE"}`;
      runtime.textContent = readiness.status === "ready"
        ? `READY · EGRESS ${readiness.egress_enabled ? "ENABLED" : "DISABLED"}`
        : "NOT READY · FAIL CLOSED";
    } catch (error) {
      runtime.textContent = `UNAVAILABLE · ${error.message}`;
      providers.replaceChildren(element("div", "provider", "Registry evidence unavailable."));
    }
  }

  async function runPlan(event) {
    if (event) event.preventDefault();
    const model = $("#model").value.trim();
    const dataClassification = $("#classification").value;
    const maxCostTier = Number.parseInt($("#cost").value, 10);
    const planState = $("#plan-state");

    if (!model) {
      planState.textContent = "INVALID MODEL";
      $("#model").focus();
      return;
    }

    planState.textContent = "PLANNING";
    output.textContent = "Computing deterministic policy order…";
    try {
      const payload = await requestJSON("/api/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model,
          data_classification: dataClassification,
          max_cost_tier: Number.isFinite(maxCostTier) ? maxCostTier : 10,
        }),
      });
      renderCandidates(payload);
      output.textContent = JSON.stringify(payload, null, 2);
      planState.textContent = payload.selected ? `SELECTED · ${payload.selected}` : "NO ELIGIBLE PROVIDER";
      $("#receipt-short").textContent = `${payload.receipt.digest.slice(0, 12)}…`;
    } catch (error) {
      candidates.className = "candidates empty";
      candidates.textContent = "Route planning failed closed.";
      output.textContent = error.message;
      planState.textContent = "BLOCKED";
    }
  }

  $("#plan-form").addEventListener("submit", runPlan);
  $("#run-plan").addEventListener("click", () => $("#plan-form").requestSubmit());
  $("#refresh").addEventListener("click", refreshRegistry);
  refreshRegistry();
})();
