# SPDX-License-Identifier: Apache-2.0
"""Read-only provider preparation on actual module/API and executed browser seams.

No provider key, network provider, device, model, billing operation or publication.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
import router_control.app as control
from router_control import free_tier

ROOT = Path(__file__).resolve().parents[1]
WHEN = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)


def fixture():
    return free_tier.dossier("a" * 40, now=WHEN)


def test_exact_candidate_identity_and_disabled_schema():
    value = fixture()
    assert [(r["id"], r["upstream_model"]) for r in value["options"]] == [
        ("zai-standard-flash47-review", "glm-4.7-flash"),
        ("zai-standard-flash45-review", "glm-4.5-flash"),
        ("groq-oss120-free-review", "openai/gpt-oss-120b"),
    ]
    providers = []
    for row in value["options"]:
        provider = control.ProviderRecord.model_validate(row["disabled_provider_proposal"])
        assert provider.enabled is False and provider.classifications == ["public"]
        assert provider.sovereignty == 0 and provider.priority == 10000
        providers.append(provider)
    registry = control.Registry(providers=providers)
    settings = control.Settings(registry, frozenset({"api.z.ai", "api.groq.com"}), True, "VALIDATED")
    for provider in providers:
        alias = next(iter(provider.models))
        assert control.route_candidates(settings, control.PlanRequest(model=alias)) == []


def test_exact_standard_endpoints_not_paid_or_region_substitution():
    rows = fixture()["options"]
    assert [r["disabled_provider_proposal"]["base_url"] for r in rows] == [
        "https://api.z.ai/api/paas/v4", "https://api.z.ai/api/paas/v4",
        "https://api.groq.com/openai/v1",
    ]
    assert all(r["disabled_provider_proposal"]["models"] for r in rows)
    assert all("tools" not in r["disabled_provider_proposal"] for r in rows)
    assert all(r["inference_measured"] is False for r in rows)


@pytest.mark.parametrize("key", [
    "dispatch_authorized", "provider_calls_performed", "registry_mutated", "production_authorized",
    "paid_fallback_authorized", "private_data_disclosure_authorized", "codex_balance_changed",
])
def test_no_derived_authority(key):
    assert fixture()[key] is False


def test_price_and_quota_do_not_claim_user_balance_or_license():
    for row in fixture()["options"]:
        assert row["remaining_quota"] is None and row["actual_charge_usd"] is None
        assert row["account_checked"] is False and row["upstream_source_revision"] is None
        assert row["basis"] == "REPORTED_NOT_SZL_MEASURED"
        assert "DISCLOSURE_AND_RETENTION_UNQUALIFIED" in row["blockers"]
        assert "ZERO_BILLING_ENFORCEMENT_UNVERIFIED" in row["blockers"]
    assert fixture()["options"][-1]["reported_quota"]["tokens_per_day"] == 200000


@pytest.mark.parametrize("now,state", [
    (datetime(2026, 10, 3, 23, 59, tzinfo=timezone.utc), "CLOCK_BEFORE_REVIEW"),
    (datetime(2026, 10, 4, tzinfo=timezone.utc), "DATED_DOCUMENTATION_REVIEW"),
    (datetime(2026, 10, 10, 23, 59, tzinfo=timezone.utc), "DATED_DOCUMENTATION_REVIEW"),
    (datetime(2026, 10, 11, tzinfo=timezone.utc), "RECHECK_REQUIRED"),
    (datetime(2027, 1, 1, tzinfo=timezone.utc), "RECHECK_REQUIRED"),
    (datetime(2026, 10, 10, 21, tzinfo=timezone(timedelta(hours=-4))), "RECHECK_REQUIRED"),
])
def test_expiry_never_enables_anything(now, state):
    report = free_tier.dossier(now=now)
    assert report["evidence_state"] == state
    assert report["reviewed_on"] == "2026-10-04"
    assert report["disposition"] == "HOLD" and report["dispatch_authorized"] is False
    for item in report["options"]:
        assert item["disabled_provider_proposal"]["enabled"] is False
        if state != "DATED_DOCUMENTATION_REVIEW":
            assert state in item["blockers"]


@pytest.mark.parametrize("value", [datetime(2026, 10, 4), "now", True, 1])
def test_unusable_clock_not_silently_normalized(value):
    with pytest.raises(ValueError):
        free_tier.dossier(now=value)


def test_semantic_key_excludes_generation_time_but_retains_expiry_and_source():
    first = free_tier.dossier("a" * 40, now=WHEN)
    second = free_tier.dossier("a" * 40, now=WHEN + timedelta(hours=1))
    assert first == second
    assert first["semantic_progress_key"] != free_tier.dossier("b" * 40, now=WHEN)["semantic_progress_key"]
    assert first["semantic_progress_key"] != free_tier.dossier("a" * 40, now=WHEN + timedelta(days=7))["semantic_progress_key"]
    content = {k: v for k, v in first.items() if k != "semantic_progress_key"}
    assert first["semantic_progress_key"] == hashlib.sha256(free_tier._canonical(content)).hexdigest()


@pytest.mark.parametrize("revision", [None, True, "main", "a" * 39, "a" * 41, "a" * 63, "X" * 40, "secret-fixture"])
def test_invalid_source_is_not_echoed_or_upgraded(revision):
    result = free_tier.dossier(revision, now=WHEN)
    assert result["source_revision"] == "UNAVAILABLE"
    assert "secret-fixture" not in json.dumps(result)


def test_returned_data_cannot_mutate_next_dossier():
    report = fixture()
    report["options"][0]["disabled_provider_proposal"]["enabled"] = True
    report["options"][0]["primary_urls"].append("untrusted-fixture")
    assert fixture() != report
    assert fixture()["options"][0]["disabled_provider_proposal"]["enabled"] is False


def test_bounded_module_cli_and_no_environment_reflection(monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "secret-fixture-not-to-publish")
    result = subprocess.run([sys.executable, "-m", "router_control.free_tier"],
                            cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and result.stderr == ""
    assert "secret-fixture-not-to-publish" not in result.stdout
    assert len(result.stdout.encode()) <= free_tier.MAX_DOSSIER_BYTES
    assert json.loads(result.stdout)["disposition"] == "HOLD"


def test_real_mounted_get_has_no_registry_or_transport_side_effect(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("read-only options accessed runtime/provider authority")
    monkeypatch.setattr(control, "load_settings", forbidden)
    monkeypatch.setattr(control, "credential_state", forbidden)
    monkeypatch.setattr(control, "call_provider", forbidden)
    monkeypatch.setenv("SOURCE_REVISION", "a" * 40)
    monkeypatch.setenv("ZAI_API_KEY", "secret-fixture-not-to-publish")
    with TestClient(control.app) as client:
        response = client.get("/api/free-provider-options")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["source_revision"] == "a" * 40
        assert response.json()["dispatch_authorized"] is False
        assert "secret-fixture" not in response.text
        assert client.post("/api/free-provider-options", json={"enabled": True}).status_code == 405


def test_proposals_are_not_registered_even_with_credentials(monkeypatch):
    monkeypatch.delenv("SZL_ROUTER_PROVIDERS_JSON", raising=False)
    monkeypatch.delenv("SZL_ROUTER_ENABLE_EGRESS", raising=False)
    monkeypatch.setenv("ZAI_API_KEY", "synthetic-unused-key")
    with TestClient(control.app) as client:
        before = client.get("/api/routes").json()
        assert client.get("/api/free-provider-options").status_code == 200
        after = client.get("/api/routes").json()
        assert before == after and before["providers"] == []
        result = client.post("/v1/chat/completions", json={
            "model": "zai-flash47-review", "messages": [{"role": "user", "content": "synthetic"}],
        })
        assert result.status_code == 503 and result.json()["detail"]["code"] == "EGRESS_DISABLED"


def test_new_assets_and_module_in_actual_source_witness():
    with TestClient(control.app) as client:
        source = client.get("/api/source").json()
        for path in ("router_control/free_tier.py", "router_control/static/free-options.js", "router_control/static/free-options.css"):
            assert source["controlled_files"][path] == hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        for filename in ("free-options.js", "free-options.css"):
            response = client.get("/static/" + filename)
            assert response.status_code == 200
            assert response.content == (ROOT / "router_control" / "static" / filename).read_bytes()


def test_existing_html_integration_and_source_no_external_execution():
    static = ROOT / "router_control" / "static"
    html = (static / "index.html").read_text()
    assert 'id="free-options-panel"' in html and 'aria-labelledby="free-options-title"' in html
    assert 'src="/static/free-options.js"' in html
    assert 'href="/static/free-options.css"' in html
    assert 'id="completion-form"' in html and 'src="/static/app.js"' in html
    js = (static / "free-options.js").read_text()
    for forbidden in ("innerHTML", "localStorage", "sessionStorage", "document.cookie", "/v1/chat/completions", "Authorization:", "WebSocket(", "EventSource(", "eval("):
        assert forbidden not in js
    assert 'fetch("/api/free-provider-options"' in js
    assert 'credentials: "omit"' in js and 'redirect: "error"' in js


HARNESS = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const data = JSON.parse(process.argv[2]);
const scenario = process.argv[3];
class Element {
 constructor() { this.children=[]; this._text=''; this.listeners={}; this.disabled=false; this.attrs={}; }
 set textContent(v) { this._text=String(v); this.children=[]; }
 get textContent() { return this._text+this.children.map(n=>n.textContent).join(' '); }
 set innerHTML(v) { throw Error('HTML injection'); }
 append(...nodes) { this.children.push(...nodes); }
 replaceChildren(...nodes) { this._text=''; this.children=nodes; }
 setAttribute(k,v) { this.attrs[k]=v; }
 addEventListener(k,v) { this.listeners[k]=v; }
}
const nodes=Object.fromEntries(['panel','load','state','evidence','list'].map(k=>['free-options-'+k,new Element()]));
const calls=[]; let fetchFn, timer; let timers=0;
if (scenario==='enabled') data.options[0].disabled_provider_proposal.enabled=true;
if (scenario==='paid') data.options[0].upstream_model='glm-4.7-flashx';
if (scenario==='duplicate') data.options[2]=data.options[0];
if (scenario==='missing-authority') delete data.dispatch_authorized;
if (scenario==='grant') data.paid_fallback_authorized=true;
if (scenario==='empty') data.options=[];
if (scenario==='expired') data.evidence_state='RECHECK_REQUIRED';
if (scenario==='xss') data.options[0].provider_label='<img src=x onerror=alert(1)>';
fetchFn=async()=>new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}});
if (scenario==='html') fetchFn=async()=>new Response('<html>fixture</html>',{headers:{'Content-Type':'text/html'}});
if (scenario==='status') fetchFn=async()=>new Response('unavailable',{status:503});
if (scenario==='overflow') fetchFn=async()=>new Response('x'.repeat(32769),{headers:{'Content-Type':'application/json'}});
if (scenario==='malformed') fetchFn=async()=>new Response('{broken',{headers:{'Content-Type':'application/json'}});
if (scenario==='utf8') fetchFn=async()=>new Response(new Uint8Array([255]),{headers:{'Content-Type':'application/json'}});
const context={document:{getElementById:id=>nodes[id],createElement:()=>new Element()},AbortController,TextDecoder,
 window:{setTimeout:(fn,ms)=>{assert.equal(ms,6000);timer=fn;timers++;return 1;},clearTimeout:()=>{timers--; }},
 fetch:async(url,options)=>{calls.push({url,options});assert.equal(url,'/api/free-provider-options');
 assert.equal(options.method,'GET');assert.equal(options.credentials,'omit');assert.equal(options.redirect,'error');
 assert.equal(options.body,undefined);assert.equal(options.headers.Authorization,undefined);return fetchFn(options);}};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
const click=()=>nodes['free-options-load'].listeners.click();
(async()=>{
 assert.equal(calls.length,0,'import/load must not contact even the dossier endpoint');
 if (scenario==='duplicate-click') {
  let resolve;
  fetchFn=()=>new Promise(r=>resolve=r);
  const pending=click(); await click(); assert.equal(calls.length,1);
  assert.equal(nodes['free-options-load'].disabled,true);
  resolve(new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}})); await pending;
 } else if (scenario==='abort') {
  fetchFn=options=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(Error('synthetic-abort'))));
  const pending=click();timer();await pending;
 } else if (scenario==='stale-clear') {
  await click();assert.equal(nodes['free-options-list'].children.length,3);
  fetchFn=async()=>{throw Error('private diagnostic should not render');};await click();
 } else await click();
 const good=['success','expired','xss','duplicate-click'].includes(scenario);
 assert.equal(nodes['free-options-list'].children.length,good?3:0);
 assert.equal(nodes['free-options-load'].disabled,false);
 assert.equal(nodes['free-options-panel'].attrs['aria-busy'],'false');assert.equal(timers,0);
 if (good) assert(nodes['free-options-state'].textContent.includes('ALL HOLD'));
 else {assert(nodes['free-options-state'].textContent.includes('UNAVAILABLE'));assert.equal(nodes['free-options-evidence'].textContent,'');}
 if(scenario==='expired')assert(nodes['free-options-state'].textContent.includes('RECHECK_REQUIRED'));
 if(scenario==='xss')assert(nodes['free-options-list'].textContent.includes('<img src=x onerror=alert(1)>'));
 assert(!Object.values(nodes).some(n=>n.textContent.includes('private diagnostic')));
 console.log('PASS '+scenario);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''


@pytest.mark.parametrize("scenario", [
    "success", "enabled", "paid", "duplicate", "missing-authority", "grant", "empty", "expired", "xss",
    "html", "status", "overflow", "malformed", "utf8", "duplicate-click", "abort", "stale-clear",
])
def test_executed_browser_boundaries(scenario):
    node = shutil.which("node")
    assert node, "Node.js is required; the browser seam suite cannot silently skip"
    result = subprocess.run([node, "-e", HARNESS, str(ROOT / "router_control/static/free-options.js"),
                             json.dumps(fixture()), scenario], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "PASS " + scenario in result.stdout
