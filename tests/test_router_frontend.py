# SPDX-License-Identifier: Apache-2.0
"""Execute the shipped browser script against controlled DOM and transport seams.

Node is required so credential, failure and receipt assertions cannot silently skip.
No provider calls, browser packages or dependencies are needed for these tests.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "router_control" / "static"


def node_runtime() -> str:
    node = shutil.which("node")
    assert node, "Node.js is required to execute frontend security and receipt checks"
    return node


def test_browser_script_syntax() -> None:
    result = subprocess.run(
        [node_runtime(), "--check", str(STATIC / "app.js")],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr


HARNESS = r'''
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const script = fs.readFileSync(process.argv[1], "utf8");
const ids = JSON.parse(process.argv[2]);
const scenario = process.argv[3];

class Element {
  constructor() {
    this.children = []; this.listeners = {}; this.value = ""; this.disabled = false;
    this.classList = { add: () => {} }; this.attributes = {}; this._text = "";
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(" "); }
  set innerHTML(value) { throw new Error("Unsafe HTML rendering"); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ""; this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  reportValidity() { return true; }
  focus() {}
  requestSubmit() { return this.fire("submit"); }
  fire(name) { return this.listeners[name]({ preventDefault() {} }); }
}

const response = (payload, status = 200) => ({
  ok: status >= 200 && status < 300, status, text: async () => JSON.stringify(payload),
  headers: { get: () => payload && payload.szl_receipt ? payload.szl_receipt.digest : null },
});
const completion = {
  choices: [{ message: { role: "assistant", content: "<img src=x onerror=alert(1)>" }, finish_reason: "stop" }],
  szl_receipt: { digest: "a".repeat(64), attempts: [{ provider_id: "local-test", state: "SUCCESS", status_code: 200 }] },
};
const consistent = {
  status: "CONSISTENT", checks: { receipt_digest: true, response_digest: true, request_digest: true, header_digest: true },
  trust: "UNSIGNED_HONEST", identity_verified: false,
};

async function setup({ secure = true, hostname = "router.example.test", protocol = "https:" } = {}) {
  const nodes = Object.fromEntries(ids.map(id => ["#" + id, new Element()]));
  nodes["#model"].value = "szl-default"; nodes["#classification"].value = "internal";
  nodes["#cost"].value = "4"; nodes["#max-tokens"].value = "512";
  const calls = []; const timers = new Map(); let timerId = 0; const clock = { now: 0 };
  const handlers = {
    "/api/routes": () => response({ state: "VALIDATED", egress_enabled: true, providers: [] }),
    "/api/plan": () => response({ candidates: [], selected: null, receipt: { digest: "plan-digest" } }),
    "/api/source": () => response({ revision: "b".repeat(40), receipt: { digest: "source-proof" } }),
    "/readyz": () => response({ status: "ready", inference: { ready_for_requests: true, basis: "LOCAL_CONFIGURATION_ONLY", checks: { registry_valid: true } } }),
    "/api/local-models": () => response({ status: "unavailable", basis: "LOCAL_DAEMON_REPORTED", observation_state: "NO_PROBE", observed_at: null, observation_age_ms: null, providers: [] }),
    "/v1/chat/completions": () => response(completion),
    "/api/verify": () => response(consistent),
  };
  const context = {
    document: {
      querySelector: selector => {
        assert(nodes[selector], "script references missing HTML element: " + selector);
        return nodes[selector];
      },
      createElement: () => new Element(),
    },
    window: {
      isSecureContext: secure, location: { hostname, protocol },
      performance: { now: () => clock.now },
      setTimeout: (fn, delay) => { const id = ++timerId; timers.set(id, { fn, delay }); return id; },
      clearTimeout: id => timers.delete(id),
    },
    AbortController,
    fetch: async (url, options) => {
      calls.push({ url, options });
      assert(url.startsWith("/"), "browser request must remain on same origin");
      assert.equal(options.redirect, "error", "caller token requests must reject redirects");
      if (url === "/v1/chat/completions") assert.equal(nodes["#caller-token"].value, "", "token is cleared before fetch");
      assert(handlers[url], "unexpected request: " + url);
      return handlers[url](options);
    },
  };
  vm.runInNewContext(script, context);
  await new Promise(resolve => setImmediate(resolve));
  const enter = () => { nodes["#prompt"].value = "Bounded synthetic request"; nodes["#caller-token"].value = "test-caller-secret"; };
  return { nodes, calls, handlers, timers, clock, enter };
}

async function run() {
  if (scenario === "success") {
    const s = await setup(); s.enter(); let release;
    s.handlers["/v1/chat/completions"] = () => new Promise(resolve => { release = resolve; });
    const pending = s.nodes["#completion-form"].fire("submit");
    assert.equal(s.nodes["#send-completion"].disabled, true);
    await s.nodes["#completion-form"].fire("submit");
    assert.equal(s.calls.filter(call => call.url === "/v1/chat/completions").length, 1);
    release(response(completion)); await pending;
    const sent = s.calls.find(call => call.url === "/v1/chat/completions");
    const verified = s.calls.find(call => call.url === "/api/verify");
    const request = JSON.parse(sent.options.body);
    assert.equal(sent.options.headers.Authorization, "Bearer test-caller-secret");
    assert.equal(request.model, "szl-default"); assert.equal(request.data_classification, "internal");
    assert.equal(request.max_cost_tier, 4); assert.equal(request.max_tokens, 512); assert.equal(request.stream, false);
    assert.deepEqual(JSON.parse(verified.options.body), { completion, request });
    assert.equal(verified.options.headers.Authorization, undefined);
    assert.equal(verified.options.headers["X-SZL-Receipt"], completion.szl_receipt.digest);
    assert(s.nodes["#answer-output"].textContent.includes("<img src=x onerror=alert(1)>"));
    assert(s.nodes["#verification-state"].textContent.includes("CONSISTENT · UNSIGNED_HONEST"));
    assert(s.nodes["#completion-model-identity"].textContent.includes("no local identity claim"));
    assert.equal(s.nodes["#send-completion"].disabled, false);
    assert.equal(s.nodes["#completion-form"].attributes["aria-busy"], "false");
    assert(!Object.values(s.nodes).some(node => node.textContent.includes("test-caller-secret")));
  } else if (scenario === "insecure") {
    const s = await setup({ secure: false, protocol: "http:" }); s.enter();
    assert.equal(s.nodes["#caller-token"].disabled, true);
    await s.nodes["#completion-form"].fire("submit");
    assert.equal(s.calls.filter(call => call.url === "/v1/chat/completions").length, 0);
    assert.equal(s.nodes["#caller-token"].value, "");
    assert(s.nodes["#completion-state"].textContent.includes("SECURE TRANSPORT REQUIRED"));
  } else if (scenario === "loopback") {
    const s = await setup({ secure: false, hostname: "localhost", protocol: "http:" }); s.enter();
    await s.nodes["#completion-form"].fire("submit");
    assert.equal(s.calls.filter(call => call.url === "/v1/chat/completions").length, 1);
  } else if (scenario === "readiness") {
    const s = await setup();
    s.handlers["/readyz"] = () => response({ status: "not-ready", inference: { ready_for_requests: false, basis: "LOCAL_CONFIGURATION_ONLY" } }, 503);
    await s.nodes["#refresh"].fire("click");
    assert.equal(s.nodes["#registry-state"].textContent, "VALIDATED");
    assert(s.nodes["#source-proof"].textContent.includes("source-proof"));
    assert(s.nodes["#control-evidence"].textContent.includes("HTTP 503"));
    assert(s.nodes["#inference-state"].textContent.includes("UNAVAILABLE"));
    for (const path of ["/api/routes", "/api/source", "/readyz"]) s.handlers[path] = () => { throw new Error("offline"); };
    await s.nodes["#refresh"].fire("click");
    for (const id of ["registry-state", "provider-count", "egress-state"]) assert.equal(s.nodes["#" + id].textContent, "UNAVAILABLE");
    assert.equal(s.nodes["#revision"].textContent, "revision UNAVAILABLE");
    assert(!s.nodes["#source-proof"].textContent.includes("source-proof"));
    assert.equal(s.nodes["#inference-checks"].textContent, "");
  } else if (scenario === "local-ready") {
    const s = await setup();
    s.nodes["#classification"].value = "public";
    const digest = "c".repeat(64);
    s.handlers["/api/routes"] = () => response({ state: "VALIDATED", egress_enabled: true, providers: [{
      id: "on-host", provider_type: "ollama_loopback", models: ["a11oy-mini-r2"], enabled: true,
      credential_state: "NOT_REQUIRED_LOOPBACK", endpoint_state: "FIXED_LOOPBACK_UNVERIFIED",
      model_identity_state: "PIN_CONFIGURED_UNVERIFIED", model_digests: { "a11oy-mini-r2": digest },
      sovereignty: 100, priority: 1, cost_tier: 0, classifications: ["public"],
    }] });
    s.handlers["/api/local-models"] = () => response({ status: "observed", basis: "LOCAL_DAEMON_REPORTED", observation_state: "LIVE_PROBE", observed_at: "2026-10-01T12:00:00Z", observation_age_ms: 0, providers: [{
      provider_id: "on-host", inventory_state: "REACHABLE", models: [{
        public_model: "a11oy-mini-r2", upstream_model: "a11oy-mini-r2", expected_digest: digest,
        observed_digest: digest, resident_digest: null, state: "MATCH", resident_state: "NOT_LOADED", license_state: "NOT_VERIFIED",
      }],
    }] });
    await s.nodes["#refresh"].fire("click");
    assert(s.nodes["#local-provider-state"].textContent.includes("REACHABLE · LIVE_PROBE · LOCAL_DAEMON_REPORTED"));
    assert(s.nodes["#providers"].textContent.includes("PIN_CONFIGURED_UNVERIFIED"));
    const controls = s.nodes["#providers"].children[0].children.find(child => child.className === "provider-models");
    assert(controls && controls.children.length === 1, "a pinned installed model may be selected before its first load");
    assert(controls.textContent.includes("installed, not loaded"));
    await controls.children[0].fire("click");
    assert.equal(s.nodes["#model"].value, "a11oy-mini-r2");
    assert(s.nodes["#provider-binding"].textContent.includes("Required local provider on-host. No cloud fallback"));
    assert(s.nodes["#model-identity-state"].textContent.includes("INSTALLED, NOT LOADED · LIVE_PROBE · LOCAL_DAEMON_REPORTED"));
    assert(s.nodes["#model-identity-detail"].textContent.includes("license NOT_VERIFIED"));
    assert(s.nodes["#model-identity-detail"].textContent.includes("No model weight byte attestation"));
    await s.nodes["#plan-form"].fire("submit");
    const planned = s.calls.find(call => call.url === "/api/plan");
    assert.equal(JSON.parse(planned.options.body).required_provider_id, "on-host");
    const localCompletion = { ...completion, szl_receipt: { ...completion.szl_receipt, model_identity: {
      state: "LOCAL_DAEMON_REPORTED_MATCH", expected_digest: digest,
      pre_request_manifest_digest: digest, pre_request_resident_digest: null,
      post_request_manifest_digest: digest, post_request_resident_digest: digest,
      independent_attestation: "UNAVAILABLE", license_state: "NOT_VERIFIED",
    } } };
    s.handlers["/v1/chat/completions"] = () => response(localCompletion);
    s.enter(); await s.nodes["#completion-form"].fire("submit");
    const sent = s.calls.find(call => call.url === "/v1/chat/completions");
    assert.equal(JSON.parse(sent.options.body).model, "a11oy-mini-r2");
    assert.equal(JSON.parse(sent.options.body).required_provider_id, "on-host");
    assert.equal(sent.options.headers.Authorization, "Bearer test-caller-secret");
    assert(s.calls.some(call => call.url === "/api/verify"));
    assert(s.nodes["#completion-model-identity"].textContent.includes("LOCAL_DAEMON_REPORTED_MATCH · CONTENT CONSISTENT"));
    assert(s.nodes["#completion-model-evidence"].textContent.includes("license NOT_VERIFIED"));
    assert(s.nodes["#completion-model-evidence"].textContent.includes("No model weight byte attestation"));
    assert(s.calls.every(call => call.url.startsWith("/")), "browser only contacts the router origin");
    s.nodes["#classification"].value = "restricted";
    await s.nodes["#classification"].fire("input");
    assert(!s.nodes["#providers"].textContent.includes("Use a11oy-mini-r2"));
    assert(s.nodes["#provider-binding"].textContent.includes("No cloud fallback"));
    s.nodes["#model"].value = "another-alias";
    await s.nodes["#model"].fire("input");
    assert(s.nodes["#provider-binding"].textContent.includes("No provider bound"));
    await s.nodes["#plan-form"].fire("submit");
    const nextPlan = s.calls.filter(call => call.url === "/api/plan").at(-1);
    assert.equal(JSON.parse(nextPlan.options.body).required_provider_id, undefined);
  } else if (scenario === "local-unavailable") {
    const s = await setup();
    s.nodes["#model"].value = "a11oy-mini-r2";
    s.handlers["/api/routes"] = () => response({ state: "VALIDATED", egress_enabled: true, providers: [{
      id: "on-host", provider_type: "ollama_loopback", models: ["a11oy-mini-r2"], enabled: true,
      credential_state: "NOT_REQUIRED_LOOPBACK", model_identity_state: "PIN_CONFIGURED_UNVERIFIED",
      model_digests: { "a11oy-mini-r2": "c".repeat(64) },
      sovereignty: 100, priority: 1, cost_tier: 0, classifications: ["public"],
    }] });
    s.handlers["/api/local-models"] = () => response({ status: "unavailable", basis: "LOCAL_DAEMON_REPORTED", observation_state: "NO_PROBE", observed_at: null, observation_age_ms: null, providers: [{
      provider_id: "on-host", inventory_state: "UNAVAILABLE", models: [{
        public_model: "a11oy-mini-r2", expected_digest: "c".repeat(64), observed_digest: null,
        resident_digest: null, state: "UNAVAILABLE", resident_state: "UNAVAILABLE", license_state: "NOT_VERIFIED",
      }],
    }] });
    await s.nodes["#refresh"].fire("click");
    assert(s.nodes["#local-provider-state"].textContent.includes("UNAVAILABLE"));
    assert(s.nodes["#model-identity-state"].textContent.includes("no usable local observation"));
    assert(!s.nodes["#providers"].children[0].children.some(child => child.className === "provider-models"));
    s.handlers["/api/local-models"] = () => { throw new Error("offline"); };
    await s.nodes["#refresh"].fire("click");
    assert(s.nodes["#local-provider-state"].textContent.includes("observation failed"));
    assert(!s.nodes["#providers"].textContent.includes("Use a11oy-mini-r2"));
  } else if (scenario === "local-mismatch") {
    const s = await setup();
    s.handlers["/api/routes"] = () => response({ state: "VALIDATED", egress_enabled: true, providers: [{
      id: "on-host", provider_type: "ollama_loopback", models: ["a11oy-mini-r2"], enabled: true,
      credential_state: "NOT_REQUIRED_LOOPBACK", model_digests: { "a11oy-mini-r2": "c".repeat(64) },
      sovereignty: 100, priority: 1, cost_tier: 0, classifications: ["public"],
    }] });
    s.handlers["/api/local-models"] = () => response({ status: "observed", basis: "LOCAL_DAEMON_REPORTED", observation_state: "LIVE_PROBE", observed_at: "2026-10-01T12:00:00Z", observation_age_ms: 0, providers: [{
      provider_id: "on-host", inventory_state: "REACHABLE", models: [{
        public_model: "a11oy-mini-r2", expected_digest: "c".repeat(64), observed_digest: "d".repeat(64),
        resident_digest: null, state: "DIGEST_MISMATCH", resident_state: "UNAVAILABLE", license_state: "NOT_VERIFIED",
      }],
    }] });
    await s.nodes["#refresh"].fire("click");
    assert(s.nodes["#providers"].textContent.includes("No local model is currently eligible"));
    assert(!s.nodes["#providers"].textContent.includes("Use a11oy-mini-r2"));
  } else if (scenario === "local-cached-busy") {
    const s = await setup(); s.nodes["#classification"].value = "public";
    const digest = "c".repeat(64);
    s.handlers["/api/routes"] = () => response({ state: "VALIDATED", egress_enabled: true, providers: [{
      id: "on-host", provider_type: "ollama_loopback", models: ["a11oy-mini-r2"], enabled: true,
      credential_state: "NOT_REQUIRED_LOOPBACK", model_digests: { "a11oy-mini-r2": digest },
      sovereignty: 100, priority: 1, cost_tier: 0, classifications: ["public"],
    }] });
    const cached = { status: "observed", basis: "LOCAL_DAEMON_REPORTED", observation_state: "CACHED_RECENT",
      observed_at: "2026-10-01T12:00:00Z", observation_age_ms: 1250, providers: [{
        provider_id: "on-host", inventory_state: "REACHABLE", models: [{ public_model: "a11oy-mini-r2",
          expected_digest: digest, observed_digest: digest, resident_digest: null,
          state: "MATCH", resident_state: "NOT_LOADED", license_state: "NOT_VERIFIED" }],
      }],
    };
    s.handlers["/api/local-models"] = () => response(cached);
    await s.nodes["#refresh"].fire("click");
    assert(s.nodes["#local-provider-state"].textContent.includes("CACHED_RECENT · 1250 ms old"));
    assert(s.nodes["#local-provider-detail"].textContent.includes("rechecks local model identity"));
    assert(s.nodes["#providers"].textContent.includes("Use a11oy-mini-r2"));
    s.handlers["/api/local-models"] = () => response({ ...cached, observation_age_ms: 2501 });
    await s.nodes["#refresh"].fire("click");
    assert(s.nodes["#local-provider-state"].textContent.includes("UNAVAILABLE · OBSERVATION_EXPIRED · 2501 ms old"));
    assert(!s.nodes["#providers"].textContent.includes("Use a11oy-mini-r2"));
    s.handlers["/api/local-models"] = () => response({ ...cached, status: "unavailable", observation_state: "BUSY",
      observed_at: null, observation_age_ms: null, providers: [{ provider_id: "on-host", inventory_state: "BUSY",
        models: [{ public_model: "a11oy-mini-r2", expected_digest: digest, observed_digest: null,
          resident_digest: null, state: "UNAVAILABLE", resident_state: "UNAVAILABLE", license_state: "NOT_VERIFIED" }],
    }] });
    await s.nodes["#refresh"].fire("click");
    assert(s.nodes["#local-provider-state"].textContent.includes("UNAVAILABLE · BUSY"));
    assert(s.nodes["#model-identity-state"].textContent.includes("no usable local observation"));
    assert(!s.nodes["#providers"].textContent.includes("Use a11oy-mini-r2"));
  } else if (scenario === "local-clock-expiry") {
    const s = await setup(); s.nodes["#classification"].value = "public";
    const digest = "c".repeat(64);
    s.handlers["/api/routes"] = () => response({ state: "VALIDATED", egress_enabled: true, providers: [{
      id: "on-host", provider_type: "ollama_loopback", models: ["a11oy-mini-r2"], enabled: true,
      credential_state: "NOT_REQUIRED_LOOPBACK", model_digests: { "a11oy-mini-r2": digest },
      sovereignty: 100, priority: 1, cost_tier: 0, classifications: ["public"],
    }] });
    s.handlers["/api/local-models"] = () => response({ status: "observed", basis: "LOCAL_DAEMON_REPORTED",
      observation_state: "LIVE_PROBE", observed_at: "2026-10-01T12:00:00Z", observation_age_ms: 0,
      providers: [{ provider_id: "on-host", inventory_state: "REACHABLE", models: [{
        public_model: "a11oy-mini-r2", expected_digest: digest, observed_digest: digest,
        resident_digest: null, state: "MATCH", resident_state: "NOT_LOADED", license_state: "NOT_VERIFIED",
      }] }],
    });
    await s.nodes["#refresh"].fire("click");
    const button = s.nodes["#providers"].children[0].children.find(child => child.className === "provider-models").children[0];
    s.clock.now = 2001;
    await button.fire("click");
    assert.equal(s.nodes["#model"].value, "szl-default", "stale click must not bind the model");
    assert(s.nodes["#provider-binding"].textContent.includes("No provider bound"));
    assert(s.nodes["#local-provider-state"].textContent.includes("UNAVAILABLE · OBSERVATION_EXPIRED"));
    assert(!s.nodes["#providers"].textContent.includes("Use a11oy-mini-r2"));
    await s.nodes["#refresh"].fire("click");
    assert(s.nodes["#providers"].textContent.includes("Use a11oy-mini-r2"));
    const expiry = [...s.timers.values()].find(timer => timer.delay >= 1 && timer.delay <= 2000);
    assert(expiry, "fresh local observation must schedule automatic expiry");
    s.clock.now = 4002;
    expiry.fn();
    assert(s.nodes["#local-provider-state"].textContent.includes("UNAVAILABLE · OBSERVATION_EXPIRED"));
    assert(!s.nodes["#providers"].textContent.includes("Use a11oy-mini-r2"));
  } else if (scenario === "divergent") {
    const s = await setup(); s.enter();
    s.handlers["/api/verify"] = () => response({ ...consistent, status: "DIVERGENT", checks: { ...consistent.checks, response_digest: false } }, 422);
    await s.nodes["#completion-form"].fire("submit");
    assert(s.nodes["#verification-state"].textContent.includes("DIVERGENT"));
    assert(s.nodes["#completion-model-identity"].textContent.includes("model claim not accepted"));
    assert(s.nodes["#completion-state"].textContent.includes("VERIFICATION FAILED"));
    assert(s.nodes["#answer-output"].textContent.includes("<img"));
  } else if (scenario === "verifier-unavailable") {
    const s = await setup(); s.enter();
    s.handlers["/api/verify"] = () => { throw new Error("offline"); };
    await s.nodes["#completion-form"].fire("submit");
    assert(s.nodes["#verification-state"].textContent.includes("UNAVAILABLE · UNSIGNED_HONEST"));
    assert(s.nodes["#completion-state"].textContent.includes("ANSWER RECEIVED · UNVERIFIED"));
    assert(s.nodes["#answer-output"].textContent.includes("<img"));
    assert(s.nodes["#completion-receipt"].textContent.includes("SUCCESS"));
  } else if (scenario === "missing-header") {
    const s = await setup(); s.enter();
    s.handlers["/v1/chat/completions"] = () => ({ ...response(completion), headers: { get: () => null } });
    await s.nodes["#completion-form"].fire("submit");
    assert(s.nodes["#verification-state"].textContent.includes("DIVERGENT / UNVERIFIED"));
    assert(!s.nodes["#completion-state"].textContent.includes("CONTENT CONSISTENT"));
  } else if (scenario === "precise-json") {
    const s = await setup(); s.enter();
    const raw = JSON.stringify(completion).slice(0, -1)
      + ',"usage":{"prompt_tokens":9007199254740993},"numeric_evidence":1e-07}';
    s.handlers["/v1/chat/completions"] = () => ({ ...response(completion), text: async () => raw });
    await s.nodes["#completion-form"].fire("submit");
    const verified = s.calls.find(call => call.url === "/api/verify");
    assert(verified.options.body.startsWith('{"completion":' + raw + ',"request":'));
    assert(verified.options.body.includes('"prompt_tokens":9007199254740993'));
    assert(verified.options.body.includes('"numeric_evidence":1e-07'));
    assert(s.nodes["#verification-state"].textContent.includes("CONSISTENT · UNSIGNED_HONEST"));
  } else if (scenario === "failure") {
    const s = await setup(); s.enter();
    s.handlers["/v1/chat/completions"] = () => response({ detail: {
      state: "ALL_ELIGIBLE_PROVIDERS_FAILED", digest: "failure-digest", secret_material_recorded: false,
      attempts: [{ provider_id: "first", state: "SKIPPED_CREDENTIAL_UNAVAILABLE" }, { provider_id: "second", state: "UPSTREAM_HTTP_ERROR", status_code: 429 }],
    } }, 502);
    await s.nodes["#completion-form"].fire("submit");
    assert(s.nodes["#attempts"].textContent.includes("SKIPPED_CREDENTIAL_UNAVAILABLE"));
    assert(s.nodes["#attempts"].textContent.includes("HTTP 429"));
    assert(s.nodes["#completion-receipt"].textContent.includes("failure-digest"));
    assert(s.nodes["#completion-state"].textContent.includes("NO ANSWER ESTABLISHED"));
    assert.equal(s.calls.filter(call => call.url === "/api/verify").length, 0);
  } else if (scenario === "timeout") {
    const s = await setup(); s.enter();
    s.handlers["/v1/chat/completions"] = options => new Promise((resolve, reject) => {
      options.signal.addEventListener("abort", () => reject(Object.assign(new Error("abort"), { name: "AbortError" })));
    });
    const pending = s.nodes["#completion-form"].fire("submit");
    const timer = [...s.timers.values()].find(timer => timer.delay >= 720000);
    assert(timer, "completion timeout must cover the bounded backend provider budget");
    timer.fn(); await pending;
    assert(s.nodes["#answer-output"].textContent.includes("Provider outcome is unknown"));
    assert.equal(s.nodes["#send-completion"].disabled, false);
    assert.equal(s.nodes["#caller-token"].value, "");
    assert.equal(s.timers.size, 0);
  } else if (scenario === "bounds") {
    const s = await setup(); s.enter();
    s.nodes["#prompt"].value = "a".repeat(32001);
    await s.nodes["#completion-form"].fire("submit");
    s.nodes["#prompt"].value = " ";
    await s.nodes["#completion-form"].fire("submit");
    s.nodes["#prompt"].value = "valid"; s.nodes["#max-tokens"].value = "131073";
    await s.nodes["#completion-form"].fire("submit");
    assert.equal(s.calls.filter(call => call.url === "/v1/chat/completions").length, 0);
    assert(s.nodes["#completion-state"].textContent.includes("INVALID REQUEST"));
  } else throw new Error("Unknown scenario " + scenario);
}
run().catch(error => { console.error(error); process.exitCode = 1; });
'''


@pytest.mark.parametrize(
    "scenario", ["success", "insecure", "loopback", "readiness", "local-ready", "local-unavailable", "local-mismatch", "local-cached-busy", "local-clock-expiry", "divergent", "verifier-unavailable", "missing-header", "precise-json", "failure", "timeout", "bounds"],
)
def test_browser_completion_and_evidence_boundaries(scenario: str) -> None:
    ids = re.findall(r'\bid="([^"]+)"', (STATIC / "index.html").read_text(encoding="utf-8"))
    assert len(ids) == len(set(ids)), "HTML IDs must be unique"
    result = subprocess.run(
        [node_runtime(), "-e", HARNESS, str(STATIC / "app.js"), json.dumps(ids), scenario],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
