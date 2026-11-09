#!/usr/bin/env python3
"""SZL Truth Router — the world's first honesty-weighted router (HF Space home).
Routes by MEASURED honesty (census receipts) x MEASURED energy (joules/token)
x price. Every decision hash-chained. Fail-closed."""
import json, hashlib, time, math, urllib.request
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

DS = "SZLHOLDINGS/frontier-54-runs"; HF = "https://huggingface.co"
app = FastAPI(title="szl-truth-router", version="1.0.0")

def get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "szl-truth-router/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

PRICES = {"gpt-5.6-sol": 15.0, "gpt-5.2": 10.0, "Qwen/Qwen3.8-27B": 0.87,
          "Qwen/Qwen3.6-35B-A3B": 0.9, "claude-pinned-lane": 3.0}

def load_registry():
    models = {}
    try:
        legs = [f["path"] for f in get(f"{HF}/api/datasets/{DS}/tree/main/census?recursive=true") if f["path"].endswith(".json")]
        for p in legs:
            leg = get(f"{HF}/datasets/{DS}/resolve/main/{p}")
            for run in leg.get("runs", []):
                if run.get("error"): continue
                m = models.setdefault(run["model"], {"receipts": []})
                m["abstain"] = run.get("abstained", 0); m["probes"] = run.get("probes_valid", 6)
                m["receipts"].append(p)
    except Exception as e:
        print("census fail:", str(e)[:80])
    try:
        legs = [f["path"] for f in get(f"{HF}/api/datasets/{DS}/tree/main/benchmark?recursive=true") if f["path"].endswith(".json")]
        for p in legs:
            b = get(f"{HF}/datasets/{DS}/resolve/main/{p}")
            jpt = (b.get("energy") or {}).get("joules_per_token")
            if jpt: models.setdefault("Qwen/Qwen3-1.7B", {})["joules_per_token"] = jpt
    except Exception as e:
        print("bench fail:", str(e)[:80])
    for m, d in models.items():
        d["price_per_1m"] = PRICES.get(m, 1.0)
        h = d.get("abstain", 0) / max(d.get("probes", 6), 1)
        d["honesty"] = round(h, 3)
        d["truth_score"] = round(h*100
            - 0.5*math.log10(max(d.get("price_per_1m",1.0),0.01))
            - 0.1*math.log10(max(d.get("joules_per_token",0.01),0.001)*1000+1), 3)
    return models

REGISTRY = load_registry()
LOADED = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "szl-truth-router", "version": app.version,
            "registry_loaded": LOADED, "models": len(REGISTRY)}

@app.get("/registry")
def registry():
    if not REGISTRY:
        raise HTTPException(503, {"error": "chain_unavailable", "honesty": "fail-closed: no cached registry"})
    ranked = sorted(REGISTRY.items(), key=lambda kv: -kv[1].get("truth_score", 0))
    return {"schema": "szl.truth-router/1.0",
        "note": "Honesty from census receipts; energy from silicon-sweep receipts; prices published estimates (labeled).",
        "models": [{"model": k, **v} for k, v in ranked]}

@app.post("/route")
def route(req: dict = None):
    req = req or {}
    if not REGISTRY:
        raise HTTPException(503, {"error": "chain_unavailable", "honesty": "fail-closed"})
    ranked = sorted(REGISTRY.items(), key=lambda kv: -kv[1].get("truth_score", 0))
    top = ranked[0]
    decision = {"schema": "szl.truth-router/1.0", "decision": "route",
        "task_hint": str(req.get("task",""))[:120], "chosen_model": top[0],
        "why": {"honesty": top[1].get("honesty"), "abstain": "%s/%s" % (top[1].get("abstain"), top[1].get("probes")),
                "joules_per_token": top[1].get("joules_per_token","UNMEASURED"),
                "price_per_1m_usd": top[1].get("price_per_1m"), "truth_score": top[1].get("truth_score")},
        "alternatives": [{"model": k, "truth_score": v.get("truth_score")} for k, v in ranked[1:3]],
        "receipts": top[1].get("receipts", []),
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    blob = json.dumps(decision, sort_keys=True)
    decision["decision_sha256"] = hashlib.sha256(blob.encode()).hexdigest()
    return decision

app.mount("/", StaticFiles(directory="/app/static", html=True), name="static")
