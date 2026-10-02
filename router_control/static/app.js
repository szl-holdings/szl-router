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
  let currentRegistry = null;
  let currentInference = null;
  let currentLocalReport = null;
  let localReportReceivedAtMs = null;
  let localExpiryTimer = null;
  let requiredProviderId = null;

  function monotonicNow() {
    return window.performance && typeof window.performance.now === "function"
      ? window.performance.now() : Date.now();
  }

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

  function localModel(providerId, alias) {
    const providers = currentLocalReport && Array.isArray(currentLocalReport.providers) ? currentLocalReport.providers : [];
    const provider = providers.find((item) => item.provider_id === providerId);
    const models = provider && Array.isArray(provider.models) ? provider.models : [];
    return { provider, model: models.find((item) => item.public_model === alias) };
  }

  function localObservation() {
    const report = currentLocalReport || {};
    const serverAge = report.observation_age_ms;
    const age = typeof serverAge === "number" && Number.isFinite(serverAge) && serverAge >= 0
      && typeof localReportReceivedAtMs === "number"
      ? serverAge + Math.max(0, monotonicNow() - localReportReceivedAtMs) : null;
    const observed = ["LIVE_PROBE", "CACHED_RECENT"].includes(report.observation_state);
    const usable = observed && age !== null && age < 2000;
    const ageLabel = age === null ? "age unavailable" : `${Math.round(age)} ms old`;
    const label = observed && !usable
      ? `OBSERVATION_EXPIRED · ${ageLabel}`
      : report.observation_state === "CACHED_RECENT"
        ? `CACHED_RECENT · ${ageLabel}`
        : report.observation_state || "NO_PROBE";
    return { usable, label, age };
  }

  function scheduleLocalExpiry() {
    if (localExpiryTimer !== null) window.clearTimeout(localExpiryTimer);
    localExpiryTimer = null;
    const observation = localObservation();
    if (!observation.usable) return;
    localExpiryTimer = window.setTimeout(() => {
      localExpiryTimer = null;
      if (localObservation().usable) {
        scheduleLocalExpiry();
        return;
      }
      if (currentRegistry) renderProviders(currentRegistry);
      renderLocalEvidence();
    }, Math.max(1, Math.ceil(2000 - observation.age)));
  }

  function localModelEligible(row, alias) {
    const { provider, model } = localModel(row.id, alias);
    const observation = localObservation();
    const installedMatch = model && /^[0-9a-f]{64}$/i.test(model.expected_digest || "")
      && row.model_digests && row.model_digests[alias] === model.expected_digest
      && model.expected_digest === model.observed_digest && model.state === "MATCH";
    return currentRegistry && currentRegistry.egress_enabled === true
      && currentInference && currentInference.ready_for_requests === true
      && currentLocalReport && currentLocalReport.status === "observed"
      && currentLocalReport.basis === "LOCAL_DAEMON_REPORTED"
      && observation.usable
      && row.enabled === true && provider && provider.inventory_state === "REACHABLE"
      && Array.isArray(row.classifications) && row.classifications.includes($("#classification").value)
      && Number.isInteger(Number($("#cost").value)) && row.cost_tier <= Number($("#cost").value)
      && installedMatch && ["MATCH", "NOT_LOADED", "UNAVAILABLE"].includes(model.resident_state);
  }

  function renderProviders(payload) {
    const providers = $("#providers");
    providers.replaceChildren();
    const rows = Array.isArray(payload.providers) ? payload.providers : [];
    const aliases = new Set();
    const modelList = $("#model-aliases");
    modelList.replaceChildren();
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
      const models = Array.isArray(row.models) ? row.models : [];
      models.forEach((alias) => aliases.add(alias));
      card.append(element("p", "", `Models · ${models.join(", ") || "none"}`));
      card.append(element("p", "", `Sovereignty ${row.sovereignty} · Priority ${row.priority} · Cost ${row.cost_tier}`));
      card.append(element("p", "", `Classifications · ${(row.classifications || []).join(", ")}`));
      card.append(element("p", "", row.enabled ? "Provider enabled" : "Provider disabled"));
      card.append(element("span", `state ${["AVAILABLE", "NOT_REQUIRED_LOOPBACK"].includes(row.credential_state) ? "available" : "unavailable"}`, row.credential_state || "UNAVAILABLE"));
      if (row.provider_type === "ollama_loopback") {
        card.append(element("p", "", `Local Ollama on router host · ${row.endpoint_state || "ENDPOINT_UNVERIFIED"}`));
        card.append(element("p", "", `Configured identity · ${row.model_identity_state || "UNAVAILABLE"}`));
        const digestDetails = element("details");
        digestDetails.append(element("summary", "", "Configured model digest pins"));
        digestDetails.append(element("pre", "", JSON.stringify(row.model_digests || {}, null, 2)));
        card.append(digestDetails);
        const eligible = models.filter((alias) => localModelEligible(row, alias));
        if (eligible.length) {
          const choices = element("div", "provider-models");
          eligible.slice(0, 4).forEach((alias) => {
            const residentState = localModel(row.id, alias).model.resident_state;
            const choice = element("button", "", `Use ${alias} · ${residentState === "MATCH" ? "loaded digest reported" : "installed, not loaded"}`);
            choice.type = "button";
            choice.addEventListener("click", () => {
              const latest = currentRegistry && Array.isArray(currentRegistry.providers)
                ? currentRegistry.providers.find((item) => item.id === row.id) : null;
              if (!latest || !localModelEligible(latest, alias)) {
                if (currentRegistry) renderProviders(currentRegistry);
                renderLocalEvidence();
                return;
              }
              $("#model").value = alias;
              requiredProviderId = row.id;
              $("#model-alias-note").textContent = `Selected ${alias} from local provider ${row.id}. Compute a route plan to inspect the binding before sending.`;
              updatePolicy();
              $("#prompt").focus();
            });
            choices.append(choice);
          });
          card.append(choices);
          if (eligible.length > 4) card.append(element("p", "", `${eligible.length - 4} more eligible aliases are listed in the model field.`));
        } else {
          card.append(element("p", "", "No local model is currently eligible for a completion."));
        }
      }
      providers.append(card);
    }
    [...aliases].sort().forEach((alias) => {
      const option = element("option");
      option.value = alias;
      modelList.append(option);
    });
  }

  function renderLocalEvidence() {
    const state = $("#local-provider-state");
    const detail = $("#local-provider-detail");
    const identity = $("#model-identity-state");
    const identityDetail = $("#model-identity-detail");
    if (!currentLocalReport || currentLocalReport.basis !== "LOCAL_DAEMON_REPORTED") {
      state.textContent = "UNAVAILABLE · local observation failed";
      detail.textContent = "No current local provider observation was received.";
      identity.textContent = "UNAVAILABLE · no model observation";
      identityDetail.textContent = "A model alias does not identify loaded weights.";
      return;
    }
    const providers = Array.isArray(currentLocalReport.providers) ? currentLocalReport.providers : [];
    const observation = localObservation();
    const reachable = observation.usable ? providers.filter((row) => row.inventory_state === "REACHABLE") : [];
    state.textContent = `${reachable.length ? "REACHABLE" : "UNAVAILABLE"} · ${observation.label} · LOCAL_DAEMON_REPORTED`;
    detail.textContent = providers.length
      ? `${reachable.length} of ${providers.length} configured local provider inventories reached. ${currentLocalReport.observed_at ? `Observed at ${currentLocalReport.observed_at}. ` : ""}Sending a completion rechecks local model identity before and after the answer; this observation does not establish a completed inference.`
      : `No local provider is configured on this router. Sending a completion rechecks local model identity before and after the answer.`;
    if (!observation.usable) {
      identity.textContent = "UNAVAILABLE · no usable local observation";
      identityDetail.textContent = "The local probe is busy, absent, or too old to select a model. Refresh shortly. License NOT_VERIFIED; no model weight byte attestation.";
      return;
    }
    const alias = $("#model").value.trim();
    const observations = providers.flatMap((provider) => (Array.isArray(provider.models) ? provider.models : [])
      .filter((model) => model.public_model === alias).map((model) => ({ provider, model })));
    if (!alias || !observations.length) {
      identity.textContent = "UNAVAILABLE · alias not observed locally";
      identityDetail.textContent = alias
        ? `No local model observation for ${alias}. License NOT_VERIFIED; no model weight byte attestation.`
        : "Choose a public alias to inspect its local model evidence. License NOT_VERIFIED; no model weight byte attestation.";
      return;
    }
    const observed = observations.find(({ provider, model }) => provider.inventory_state === "REACHABLE"
      && model.state === "MATCH" && model.resident_state === "MATCH")
      || observations.find(({ provider, model }) => provider.inventory_state === "REACHABLE" && model.state === "MATCH")
      || observations[0];
    const { provider, model } = observed;
    identity.textContent = model.state === "MATCH" && ["NOT_LOADED", "UNAVAILABLE"].includes(model.resident_state)
      ? `INSTALLED, NOT LOADED · ${observation.label} · LOCAL_DAEMON_REPORTED`
      : `${model.state || "UNAVAILABLE"} / ${model.resident_state || "UNAVAILABLE"} · ${observation.label} · LOCAL_DAEMON_REPORTED`;
    identityDetail.textContent = `${provider.provider_id} · configured SHA-256 ${model.expected_digest || "UNAVAILABLE"} · daemon SHA-256 ${model.observed_digest || "UNAVAILABLE"} · resident SHA-256 ${model.resident_digest || "UNAVAILABLE"} · license ${model.license_state || "NOT_VERIFIED"}. No model weight byte attestation.`;
  }

  function renderCandidates(payload) {
    const candidates = $("#candidates");
    candidates.className = "candidates";
    candidates.replaceChildren();
    const rows = Array.isArray(payload.candidates) ? payload.candidates : [];
    if (!rows.length) {
      candidates.classList.add("empty");
      candidates.textContent = requiredProviderId
        ? `Required provider ${requiredProviderId} does not satisfy this model, classification, and cost policy.`
        : "No enabled provider satisfies this model, classification, and cost policy.";
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
    return {
      model, data_classification: $("#classification").value, max_cost_tier: cost,
      ...(requiredProviderId ? { required_provider_id: requiredProviderId } : {}),
    };
  }

  function updatePolicy() {
    $("#provider-binding").textContent = requiredProviderId
      ? `Required local provider ${requiredProviderId}. No cloud fallback.`
      : "No provider bound. Policy may choose any eligible provider.";
    $("#completion-policy").textContent = `Next request: model ${$("#model").value || "UNSET"} · classification ${$("#classification").value} · maximum cost tier ${$("#cost").value}${requiredProviderId ? ` · required local provider ${requiredProviderId} · no cloud fallback` : ""}.`;
    renderLocalEvidence();
  }

  async function refreshRegistry() {
    if (refreshing) return;
    refreshing = true;
    $("#refresh").disabled = true;
    $("#runtime").textContent = "CONTROL PLANE · CHECKING";
    // Readiness can return 503 while registry and source evidence remain usable.
    const [registry, source, readiness, localReport] = await Promise.allSettled([
      requestJSON("/api/routes").then(requireOK),
      requestJSON("/api/source").then(requireOK),
      requestJSON("/readyz"),
      requestJSON("/api/local-models").then((result) => ({ ...result, receivedAtMs: monotonicNow() })),
    ]);
    currentRegistry = registry.status === "fulfilled" ? registry.value : null;
    currentLocalReport = localReport.status === "fulfilled" && localReport.value.ok ? localReport.value.payload : null;
    localReportReceivedAtMs = currentLocalReport ? localReport.value.receivedAtMs : null;
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
      currentInference = inference;
      $("#inference-state").textContent = `${inference.ready_for_requests === true ? "CONFIGURED" : "UNAVAILABLE"} · ${inference.basis || "LOCAL_CONFIGURATION_ONLY"}`;
      $("#inference-checks").textContent = Object.entries(inference.checks || {}).map(([name, passed]) => `${name}: ${passed === true ? "PASS" : "FAIL"}`).join(" · ");
    } else {
      currentInference = null;
      $("#runtime").textContent = "CONTROL PLANE · UNAVAILABLE";
      $("#control-evidence").textContent = "Control readiness evidence unavailable.";
      $("#inference-state").textContent = "UNAVAILABLE · no current configuration evidence";
      $("#inference-checks").textContent = "";
    }
    if (currentRegistry) {
      renderProviders(currentRegistry);
    } else {
      $("#providers").replaceChildren(element("div", "provider", "Registry evidence unavailable."));
      $("#model-aliases").replaceChildren();
      for (const id of ["registry-state", "provider-count", "egress-state"]) $("#" + id).textContent = "UNAVAILABLE";
    }
    renderLocalEvidence();
    scheduleLocalExpiry();
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

  function renderCompletionIdentity(receipt, consistent) {
    const state = $("#completion-model-identity");
    const detail = $("#completion-model-evidence");
    if (!consistent) {
      state.textContent = "UNVERIFIED · model claim not accepted";
      detail.textContent = "The completion receipt was not verified for this request.";
      return;
    }
    const claim = receipt && receipt.model_identity;
    if (!claim) {
      state.textContent = "UNAVAILABLE · no local identity claim";
      detail.textContent = "Content is consistent, but this completion has no loaded model identity evidence.";
      return;
    }
    const expected = claim.expected_digest;
    const reportedMatch = claim.state === "LOCAL_DAEMON_REPORTED_MATCH"
      && /^[0-9a-f]{64}$/i.test(expected || "")
      && claim.pre_request_manifest_digest === expected
      && claim.post_request_manifest_digest === expected
      && claim.post_request_resident_digest === expected
      && claim.independent_attestation === "UNAVAILABLE"
      && claim.license_state === "NOT_VERIFIED"
      && (claim.pre_request_resident_digest == null || claim.pre_request_resident_digest === expected);
    state.textContent = reportedMatch
      ? "LOCAL_DAEMON_REPORTED_MATCH · CONTENT CONSISTENT"
      : "UNVERIFIED · model identity evidence incomplete";
    detail.textContent = reportedMatch
      ? `Expected SHA-256 ${expected} · resident before ${claim.pre_request_resident_digest || "NOT_LOADED"} · resident after ${claim.post_request_resident_digest} · independent attestation ${claim.independent_attestation || "UNAVAILABLE"} · license ${claim.license_state || "NOT_VERIFIED"}. No model weight byte attestation.`
      : "The completion receipt does not establish a matching local model digest.";
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
    $("#completion-state").textContent = `REQUESTING · ${request.model} · ${request.data_classification} · COST ≤ ${request.max_cost_tier}${request.required_provider_id ? ` · REQUIRED ${request.required_provider_id} · NO CLOUD FALLBACK` : ""}`;
    $("#answer-output").textContent = "Waiting for a bounded provider outcome…";
    $("#completion-receipt").textContent = "No receipt received for this request.";
    $("#completion-model-identity").textContent = "UNAVAILABLE · awaiting completion";
    $("#completion-model-evidence").textContent = "No loaded model identity evidence received for this request.";
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
      $("#completion-model-identity").textContent = "RECEIVED · VERIFYING RECEIPT";
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
        renderCompletionIdentity(completion.szl_receipt, consistent);
        $("#completion-state").textContent = consistent ? "ANSWER RECEIVED · CONTENT CONSISTENT" : "ANSWER RECEIVED · VERIFICATION FAILED";
      } catch (error) {
        $("#verification-state").textContent = "UNAVAILABLE · UNSIGNED_HONEST";
        $("#verify-output").textContent = failureText(error);
        renderCompletionIdentity(completion.szl_receipt, false);
        $("#completion-state").textContent = "ANSWER RECEIVED · UNVERIFIED";
      }
    } catch (error) {
      $("#completion-state").textContent = "REQUEST FAILED · NO ANSWER ESTABLISHED";
      $("#answer-output").textContent = failureText(error);
      const detail = error.payload && error.payload.detail;
      $("#completion-receipt").textContent = detail ? JSON.stringify(detail, null, 2) : "No failure receipt received. Provider outcome is unknown.";
      $("#completion-model-identity").textContent = "UNAVAILABLE · no answer established";
      $("#completion-model-evidence").textContent = "No loaded model identity evidence received for this request.";
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
  $("#model").addEventListener("input", () => {
    requiredProviderId = null;
    $("#model-alias-note").textContent = "Choose a listed alias or enter one to inspect policy. A listed alias does not establish a loaded model.";
    updatePolicy();
  });
  for (const id of ["classification", "cost"]) $("#" + id).addEventListener("input", () => {
    updatePolicy();
    if (currentRegistry) renderProviders(currentRegistry);
  });
  if (!secureTransport) {
    $("#caller-token").disabled = true;
    $("#send-completion").disabled = true;
    $("#token-note").textContent = "Caller credentials require a secure browser context or a loopback local page. Open the deployed router over HTTPS before sending a token.";
    $("#completion-state").textContent = "BLOCKED · SECURE TRANSPORT REQUIRED";
  }
  updatePolicy();
  refreshRegistry();
})();
