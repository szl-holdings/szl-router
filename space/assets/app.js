/* ===========================================================
   SZL LLM Router — PUBLIC status & concept view
   The szl-router internals stay PRIVATE. This page shows only
   public status + the sovereign-first routing concept.
   - Live fetch: router/health, router/models, router/provenance
   - Honest-degrade to clearly-labeled bundled snapshots.
   Doctrine v11.
   =========================================================== */

const BASE = 'https://a11oy.net/api/a11oy/v1';
const ENDPOINTS = {
  health: { url: BASE + '/router/health', snap: 'assets/snapshot-router-health.json' },
  models: { url: BASE + '/router/models', snap: 'assets/snapshot-router-models.json' },
  provenance: { url: BASE + '/router/provenance', snap: 'assets/snapshot-router-provenance.json' },
};
const REFRESH_MS = 15000;

const $ = (s) => document.querySelector(s);
const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

const TIER_ORDER = { sovereign: 0, 'free-grid': 1, 'paid-grid': 2 };
const TIER_LABEL = { sovereign: 'sovereign · own metal', 'free-grid': 'free tier', 'paid-grid': 'paid fallback' };

let liveFlags = { health: false, models: false, provenance: false };

/* fetch one endpoint live, degrade to snapshot */
async function fetchOrSnap(name) {
  const { url, snap } = ENDPOINTS[name];
  try {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) throw new Error('status ' + res.status);
    const data = await res.json();
    liveFlags[name] = true;
    return data;
  } catch (e) {
    try {
      const res = await fetch(snap, { cache: 'no-store' });
      const data = await res.json();
      liveFlags[name] = false;
      return data;
    } catch (e2) {
      liveFlags[name] = null;
      return null;
    }
  }
}

function setSourceBadge() {
  const anyLive = Object.values(liveFlags).some((v) => v === true);
  const allLive = Object.values(liveFlags).every((v) => v === true);
  const allDown = Object.values(liveFlags).every((v) => v === null);
  const pill = $('#source-pill');
  const dot = pill.querySelector('.dot');
  const label = pill.querySelector('.source-label');

  if (allDown) {
    dot.className = 'dot down';
    label.textContent = 'OFFLINE';
    $('#data-source-note').textContent = 'Router status endpoints are unreachable. No data shown is fabricated.';
  } else if (allLive) {
    dot.className = 'dot live';
    label.textContent = 'LIVE · szl-router';
    $('#data-source-note').textContent = 'Reading live router status endpoints. This is a public status view — no private routing logic or keys are exposed.';
  } else {
    dot.className = 'dot snapshot';
    label.textContent = anyLive ? 'PARTIAL · live + snapshot' : 'SNAPSHOT · live unavailable';
    $('#data-source-note').textContent = 'Some endpoints were unreachable (network or cross-origin restriction); those panels show a clearly-labeled bundled snapshot — never fabricated data.';
  }
}

function stamp() {
  $('#updated-stamp').textContent = 'updated ' + new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
}

/* ---------- render health ---------- */
function renderHealth(data) {
  const ok = data && data.ok === true;
  $('#kpi-health').textContent = ok ? 'UP' : (data ? 'DOWN' : '—');
  $('#kpi-health').className = 'kpi-value ' + (ok ? 'teal' : 'violet');
  $('#kpi-service').textContent = (data && data.service) ? data.service : 'szl-router';
}

/* ---------- render models ---------- */
function renderModels(data) {
  const list = (data && Array.isArray(data.data)) ? data.data : [];
  $('#kpi-models').textContent = list.length || '—';
  const wrap = $('#model-chips');
  wrap.innerHTML = '';
  if (!list.length) { wrap.innerHTML = '<span class="chip dim">no models reported</span>'; return; }
  list.forEach((m) => {
    const span = document.createElement('span');
    span.className = 'chip';
    span.textContent = m.id;
    wrap.appendChild(span);
  });
}

/* ---------- render provenance (providers + logical models) ---------- */
function renderProvenance(data) {
  const providers = (data && Array.isArray(data.providers)) ? data.providers.slice() : [];
  // count sovereign-available
  const sovUp = providers.filter((p) => p.sovereign && p.available).length;
  $('#kpi-sovereign').textContent = providers.length ? `${sovUp}/${providers.filter(p=>p.sovereign).length}` : '—';

  // sort by tier then availability
  providers.sort((a, b) => (TIER_ORDER[a.tier] ?? 9) - (TIER_ORDER[b.tier] ?? 9));

  const grid = $('#provider-grid');
  grid.innerHTML = '';
  providers.forEach((p) => {
    const tierCls = p.tier === 'sovereign' ? 'sov' : (p.tier === 'free-grid' ? 'free' : 'paid');
    const tierTag = p.tier === 'sovereign' ? 'sov' : 'host';
    const card = document.createElement('article');
    card.className = 'provider-card glass';
    card.innerHTML = `
      <div class="provider-top">
        <span class="provider-name">${escapeHtml(p.provider)}</span>
        <span class="tag ${p.available ? 'ok' : 'down'}">${p.available ? 'available' : 'idle'}</span>
      </div>
      <div class="provider-meta">
        <span class="tag ${tierTag}">${escapeHtml(TIER_LABEL[p.tier] || p.tier)}</span>
        <span class="badge ${p.sovereign ? 'teal' : ''}">${p.sovereign ? 'sovereign' : 'hosted'}</span>
      </div>
      <div class="provider-meta">
        <span class="badge">energy: ${escapeHtml(p.energy_source || 'n/a')}</span>
      </div>
      <p class="provider-note">${escapeHtml(p.note || '')}</p>
    `;
    grid.appendChild(card);
  });

  // default model readout
  if (data && data.default_model) {
    $('#default-model').textContent = data.default_model;
  }
}

/* ---------- main cycle ---------- */
async function cycle() {
  const [health, models, provenance] = await Promise.all([
    fetchOrSnap('health'),
    fetchOrSnap('models'),
    fetchOrSnap('provenance'),
  ]);
  renderHealth(health);
  renderModels(models);
  renderProvenance(provenance);
  setSourceBadge();
  stamp();
}

cycle();
setInterval(cycle, REFRESH_MS);
