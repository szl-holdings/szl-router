/* SZL LLM Router — public evidence surface.
 * The public contracts expose configuration inventory only. A reachable
 * contract is not evidence that a private router, provider, or model is live.
 */

const BASE = window.location.origin + '/api/a11oy/v1';
const ENDPOINTS = {
  health: { url: BASE + '/router/health', snap: 'assets/snapshot-router-health.json' },
  models: { url: BASE + '/router/models', snap: 'assets/snapshot-router-models.json' },
  provenance: { url: BASE + '/router/provenance', snap: 'assets/snapshot-router-provenance.json' },
};
const REFRESH_MS = 15000;
const SNAPSHOT_MAX_AGE_MS = 24 * 60 * 60 * 1000;

const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (char) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[char]));

const TIER_ORDER = { sovereign: 0, 'free-grid': 1, 'paid-grid': 2 };
const TIER_LABEL = {
  sovereign: 'sovereign · own metal',
  'free-grid': 'hosted · free tier',
  'paid-grid': 'hosted · paid fallback',
};

let endpointStates = {
  health: { source: 'PENDING', transport: 'UNKNOWN', evidence: 'UNKNOWN', freshness: 'UNKNOWN' },
  models: { source: 'PENDING', transport: 'UNKNOWN', evidence: 'UNKNOWN', freshness: 'UNKNOWN' },
  provenance: { source: 'PENDING', transport: 'UNKNOWN', evidence: 'UNKNOWN', freshness: 'UNKNOWN' },
};

function evidenceState(response, data) {
  return String(
    response.headers.get('X-SZL-Evidence-State')
      || (data && data.evidence_state)
      || (data && data.measurement_state)
      || 'UNKNOWN'
  ).toUpperCase();
}

function snapshotFreshness(response, data) {
  const reported = String(
    response.headers.get('X-SZL-Freshness-State')
      || (data && data.freshness_state)
      || ''
  ).toUpperCase();
  const capturedAt = data && typeof data.captured_at === 'string' ? data.captured_at : null;
  if (reported === 'FRESH' || reported === 'STALE') {
    return { state: reported, capturedAt };
  }
  if (!capturedAt) return { state: 'UNKNOWN', capturedAt: null };

  const capturedMs = Date.parse(capturedAt);
  const ageMs = Date.now() - capturedMs;
  if (!Number.isFinite(capturedMs) || ageMs < 0) {
    return { state: 'UNKNOWN', capturedAt };
  }
  return {
    state: ageMs <= SNAPSHOT_MAX_AGE_MS ? 'FRESH' : 'STALE',
    capturedAt,
  };
}

async function fetchOrSnap(name) {
  const { url, snap } = ENDPOINTS[name];
  try {
    const response = await fetch(url, { cache: 'no-store' });
    if (!response.ok) throw new Error(`status ${response.status}`);
    const data = await response.json();
    const freshness = snapshotFreshness(response, data);
    endpointStates[name] = {
      source: 'PUBLIC_CONTRACT',
      transport: String(response.headers.get('X-SZL-Transport-State') || 'REACHABLE').toUpperCase(),
      evidence: evidenceState(response, data),
      freshness: freshness.state,
      capturedAt: freshness.capturedAt,
    };
    return data;
  } catch (contractError) {
    try {
      const response = await fetch(snap, { cache: 'no-store' });
      if (!response.ok) throw new Error(`status ${response.status}`);
      const data = await response.json();
      const freshness = snapshotFreshness(response, data);
      endpointStates[name] = {
        source: 'LOCAL_FALLBACK',
        transport: 'UNREACHABLE',
        evidence: evidenceState(response, data),
        freshness: freshness.state,
        capturedAt: freshness.capturedAt,
      };
      return data;
    } catch (snapshotError) {
      endpointStates[name] = { source: 'UNAVAILABLE', transport: 'UNREACHABLE', evidence: 'UNKNOWN' };
      return null;
    }
  }
}

function setSourceBadge() {
  const states = Object.values(endpointStates);
  const pill = $('#source-pill');
  const dot = pill.querySelector('.dot');
  const label = pill.querySelector('.source-label');
  const allContractsReachable = states.every(
    (state) => state.source === 'PUBLIC_CONTRACT' && state.transport === 'REACHABLE'
  );
  const allUnavailable = states.every((state) => state.source === 'UNAVAILABLE');
  const hasMeasuredEvidence = states.some(
    (state) => state.evidence === 'LIVE' || state.evidence === 'COMPUTED'
  );
  const hasStaleSnapshot = states.some(
    (state) => state.evidence === 'SNAPSHOT' && state.freshness === 'STALE'
  );
  const hasUnknownSnapshotAge = states.some(
    (state) => state.evidence === 'SNAPSHOT' && state.freshness === 'UNKNOWN'
  );
  const capturedAt = states.map((state) => state.capturedAt).find(Boolean);
  const capturedLabel = capturedAt ? new Date(capturedAt).toLocaleString() : 'an unknown time';

  if (allUnavailable) {
    dot.className = 'dot down';
    label.textContent = 'OFFLINE';
    $('#data-source-note').textContent = 'The public contracts and bundled snapshots are unavailable.';
  } else if (hasStaleSnapshot && !hasMeasuredEvidence) {
    dot.className = 'dot down';
    label.textContent = 'STALE SNAPSHOT';
    const transportNote = allContractsReachable
      ? 'The public status contracts are reachable, but'
      : 'At least one public status contract is unreachable, and';
    $('#data-source-note').textContent = `${transportNote} the snapshot was captured ${capturedLabel} and is older than 24 hours. Router, provider, and model reachability remain unmeasured.`;
  } else if (hasUnknownSnapshotAge && !hasMeasuredEvidence) {
    dot.className = 'dot down';
    label.textContent = 'SNAPSHOT AGE UNKNOWN';
    $('#data-source-note').textContent = 'The status surface is reachable, but snapshot freshness cannot be verified. Router, provider, and model reachability remain unmeasured.';
  } else if (allContractsReachable && !hasMeasuredEvidence) {
    dot.className = 'dot snapshot';
    label.textContent = 'REACHABLE · SNAPSHOT';
    $('#data-source-note').textContent = 'The public status contracts are reachable. Router, provider, and model reachability are not measured by this snapshot.';
  } else if (allContractsReachable && hasMeasuredEvidence) {
    dot.className = 'dot live';
    label.textContent = 'LIVE EVIDENCE';
    $('#data-source-note').textContent = 'The public contracts include current measured evidence.';
  } else {
    dot.className = 'dot snapshot';
    label.textContent = 'LOCAL SNAPSHOT';
    $('#data-source-note').textContent = 'At least one public contract was unreachable; bundled snapshot evidence is shown and labeled.';
  }
}

function stamp() {
  $('#updated-stamp').textContent = `checked ${new Date().toLocaleTimeString([], {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })}`;
}

function renderHealth(data) {
  const reachable = data && data.status_surface === 'REACHABLE';
  $('#kpi-health').textContent = reachable ? 'REACHABLE' : 'UNKNOWN';
  $('#kpi-health').className = `kpi-value ${reachable ? 'teal' : 'violet'}`;
  $('#kpi-service').textContent = data && data.router_runtime
    ? `router runtime: ${String(data.router_runtime).toLowerCase().replace('_', ' ')}`
    : 'router runtime: not measured';
}

function renderModels(data) {
  const list = data && Array.isArray(data.data) ? data.data : [];
  const configured = list.filter((model) => model.configured === true).length;
  $('#kpi-models').textContent = list.length ? `${configured}/${list.length}` : '—';
  const wrap = $('#model-chips');
  wrap.innerHTML = '';
  if (!list.length) {
    wrap.innerHTML = '<span class="chip dim">no logical aliases reported</span>';
    return;
  }
  list.forEach((model) => {
    const span = document.createElement('span');
    span.className = model.configured ? 'chip' : 'chip dim';
    span.textContent = `${model.id} · ${model.live_reachable || 'NOT_MEASURED'}`;
    wrap.appendChild(span);
  });
}

function measuredReachability(provider) {
  if (provider.live_reachable === true && provider.last_probe_at && provider.probe_receipt_id) {
    return { label: 'MEASURED REACHABLE', className: 'ok' };
  }
  if (provider.live_reachable === false && provider.last_probe_at && provider.probe_receipt_id) {
    return { label: 'MEASURED UNREACHABLE', className: 'down' };
  }
  return { label: 'NOT MEASURED', className: '' };
}

function renderProvenance(data) {
  const providers = data && Array.isArray(data.providers) ? data.providers.slice() : [];
  const sovereign = providers.filter((provider) => provider.sovereign === true);
  const configuredSovereign = sovereign.filter((provider) => provider.configured === true).length;
  $('#kpi-sovereign').textContent = providers.length ? `${configuredSovereign}/${sovereign.length}` : '—';

  providers.sort((a, b) => (TIER_ORDER[a.tier] ?? 9) - (TIER_ORDER[b.tier] ?? 9));
  const grid = $('#provider-grid');
  grid.innerHTML = '';
  providers.forEach((provider) => {
    const tierTag = provider.tier === 'sovereign' ? 'sov' : 'host';
    const reachability = measuredReachability(provider);
    const card = document.createElement('article');
    card.className = 'provider-card glass';
    card.innerHTML = `
      <div class="provider-top">
        <span class="provider-name">${escapeHtml(provider.provider_id)}</span>
        <span class="tag ${reachability.className}">${reachability.label}</span>
      </div>
      <div class="provider-meta">
        <span class="tag ${tierTag}">${escapeHtml(TIER_LABEL[provider.tier] || provider.tier)}</span>
        <span class="badge ${provider.sovereign ? 'teal' : ''}">${provider.sovereign ? 'sovereign' : 'hosted'}</span>
      </div>
      <div class="provider-meta">
        <span class="badge">${provider.configured ? 'CONFIGURED' : 'NOT CONFIGURED'}</span>
        <span class="badge">${escapeHtml(provider.provider_class || 'redacted')}</span>
      </div>
      <p class="provider-note">Provider identity and network topology are intentionally redacted.</p>
    `;
    grid.appendChild(card);
  });

  if (data && data.default_model) $('#default-model').textContent = data.default_model;
}

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
