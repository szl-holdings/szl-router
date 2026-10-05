/* SPDX-License-Identifier: Apache-2.0 */
/* Read-only source dossier. No provider registry, credentials or inference access. */
(() => {
  "use strict";
  const panel = document.getElementById("free-options-panel");
  const button = document.getElementById("free-options-load");
  const state = document.getElementById("free-options-state");
  const evidence = document.getElementById("free-options-evidence");
  const list = document.getElementById("free-options-list");
  if (!panel || !button || !state || !evidence || !list) return;
  const maxBytes = 32768;
  const models = {
    "zai-standard-flash47-review": "glm-4.7-flash",
    "zai-standard-flash45-review": "glm-4.5-flash",
    "groq-oss120-free-review": "openai/gpt-oss-120b",
  };
  let busy = false;
  const text = (value, max = 600) => typeof value === "string" && value.length > 0 && value.length <= max;
  const strings = (value, min, max) => Array.isArray(value) && value.length >= min && value.length <= max && value.every(x => text(x));
  const disabledProposal = (proposal, item) => {
    if (!proposal || proposal.enabled !== false || proposal.provider_type !== "openai_https" ||
        proposal.sovereignty !== 0 || proposal.cost_tier !== 0 || proposal.priority !== 10000 ||
        proposal.id !== item.id || !text(proposal.token_env, 96) || !text(proposal.base_url, 512) ||
        !Array.isArray(proposal.classifications) || proposal.classifications.length !== 1 ||
        proposal.classifications[0] !== "public" || !proposal.models ||
        Object.keys(proposal.models).length !== 1 || Object.values(proposal.models)[0] !== item.upstream_model) return false;
    // No record is applied. Endpoint/account qualification still belongs to the owner.
    return Object.keys(proposal).sort().join(",") === "base_url,classifications,cost_tier,enabled,id,models,priority,provider_type,sovereignty,token_env";
  };
  const valid = data => {
    if (!data || data.schema !== "szl.router-free-provider-options/v1" ||
        data.owner !== "szl-holdings/szl-router" || data.scope !== "READ_ONLY_EVALUATION_OPTIONS" ||
        data.disposition !== "HOLD" ||
        !["DATED_DOCUMENTATION_REVIEW", "RECHECK_REQUIRED", "CLOCK_BEFORE_REVIEW"].includes(data.evidence_state) ||
        !/^\d{4}-\d{2}-\d{2}$/.test(data.reviewed_on || "") || !/^\d{4}-\d{2}-\d{2}$/.test(data.recheck_on || "") ||
        !/^(?:[0-9a-f]{40}|[0-9a-f]{64}|UNAVAILABLE)$/.test(data.source_revision || "") ||
        !/^[0-9a-f]{64}$/.test(data.semantic_progress_key || "") ||
        !Array.isArray(data.options) || data.options.length !== 3) return false;
    for (const key of ["dispatch_authorized", "provider_calls_performed", "registry_mutated", "production_authorized",
      "paid_fallback_authorized", "private_data_disclosure_authorized", "codex_balance_changed"]) {
      if (data[key] !== false) return false;
    }
    const ids = new Set();
    for (const item of data.options) {
      if (!item || !Object.hasOwn(models, item.id) || ids.has(item.id) ||
          item.upstream_model !== models[item.id] || item.disposition !== "HOLD" ||
          item.basis !== "REPORTED_NOT_SZL_MEASURED" || item.account_checked !== false ||
          item.inference_measured !== false || item.remaining_quota !== null || item.actual_charge_usd !== null ||
          item.upstream_source_revision !== null || !text(item.provider_label, 96) || !text(item.price_basis, 96) ||
          !strings(item.primary_urls, 1, 4) || !strings(item.limitations, 1, 6) || !strings(item.blockers, 1, 16) ||
          !disabledProposal(item.disabled_provider_proposal, item)) return false;
      ids.add(item.id);
    }
    return true;
  };
  const element = (tag, value) => {
    const node = document.createElement(tag);
    if (value !== undefined) node.textContent = value;
    return node;
  };
  async function readBounded(response) {
    if (response.status !== 200 || !/^application\/json(?:;|$)/i.test(response.headers.get("content-type") || "")) throw Error("unavailable");
    if (!response.body || typeof response.body.getReader !== "function") throw Error("stream-unavailable");
    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8", { fatal: true });
    let count = 0, result = "";
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        count += value.byteLength;
        if (count > maxBytes) throw Error("response-limit");
        result += decoder.decode(value, { stream: true });
      }
      result += decoder.decode();
      return JSON.parse(result);
    } finally {
      try { await reader.cancel(); } catch (_) { /* no diagnostic data is rendered */ }
      reader.releaseLock();
    }
  }
  async function load() {
    if (busy) return;
    busy = true;
    button.disabled = true;
    panel.setAttribute("aria-busy", "true");
    list.replaceChildren();
    evidence.textContent = "";
    state.textContent = "READING SOURCE OPTIONS · no provider call";
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 6000);
    try {
      const response = await fetch("/api/free-provider-options", {
        method: "GET", credentials: "omit", redirect: "error", cache: "no-store",
        headers: { Accept: "application/json" }, signal: controller.signal,
      });
      const data = await readBounded(response);
      if (!valid(data)) throw Error("invalid-dossier");
      for (const item of data.options) {
        const article = element("article");
        article.className = "free-option";
        article.append(element("h3", `${item.provider_label} · ${item.upstream_model}`));
        article.append(element("p", `HOLD · ${item.price_basis}`));
        for (const note of item.limitations) article.append(element("p", note));
        article.append(element("p", `Required evidence: ${item.blockers.join("; ")}`));
        const sources = element("div");
        sources.className = "free-option-sources";
        sources.append(element("h4", "Primary references · text only, not fetched by this page"));
        for (const url of item.primary_urls) sources.append(element("p", url));
        article.append(sources);
        const details = element("details");
        details.append(element("summary", "Inspect disabled source-review proposal"));
        const proposal = element("pre", JSON.stringify(item.disabled_provider_proposal, null, 2));
        proposal.tabIndex = 0;
        details.append(proposal);
        article.append(details);
        list.append(article);
      }
      evidence.textContent = `Reviewed ${data.reviewed_on}; local recheck deadline ${data.recheck_on}. ` +
        `Source ${data.source_revision}. Content key ${data.semantic_progress_key}. Neither is provider qualification.`;
      state.textContent = `${data.evidence_state} · 3 options · ALL HOLD · quota and billing unverified`;
    } catch (_) {
      list.replaceChildren();
      evidence.textContent = "";
      state.textContent = "UNAVAILABLE · no usable options received; nothing enabled or retried";
    } finally {
      window.clearTimeout(timer);
      busy = false;
      button.disabled = false;
      panel.setAttribute("aria-busy", "false");
    }
  }
  button.addEventListener("click", load);
})();
