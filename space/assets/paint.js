/* Fail-closed organ paint bind.
 * The HUD is a function of the gate. Missing cycle JSON is UNAVAILABLE.
 * HARD_DENY / LAMBDA_VETO / DENY_DEFAULT / ESCALATE cannot paint ALLOW.
 * Lambda stays Conjecture 1. Arithmetic-would-allow is a ghost.
 */

export const CYCLE_SCHEMA = 'szl.frontier.ouroboros-cycle.v1';
export const ORGAN_CYCLE_URL = 'https://szlholdings-szl-frontier.hf.space/frontier/ouroboros-cycle.v1.json';
export const CYCLE_PATH = '/frontier/ouroboros-cycle.v1.json';

export function paintFromCycle(cycle) {
  if (!cycle) return 'UNAVAILABLE';
  if (cycle.schema !== CYCLE_SCHEMA) return 'UNAVAILABLE';
  if (cycle.productionPromotion === true) return 'DENY';
  if (cycle.lambda !== 'CONJECTURE_1' || cycle.lambdaNeverATheorem !== true) return 'DENY';
  if (cycle.authority !== 'PROPOSAL_ONLY') return 'DENY';
  if (cycle.invariantsOk !== true) return 'DENY';
  if (cycle.shadow && cycle.shadow.executable === true) return 'DENY';
  if (cycle.verdict === 'ALLOW') return 'ALLOW';
  if (
    cycle.verdict === 'HARD_DENY' ||
    cycle.verdict === 'LAMBDA_VETO' ||
    cycle.verdict === 'DENY_DEFAULT' ||
    cycle.verdict === 'ESCALATE'
  ) {
    return 'DENY';
  }
  return 'UNAVAILABLE';
}

export function allowChrome(paint) {
  return paint === 'ALLOW';
}

export function paintTone(paint) {
  if (paint === 'ALLOW') return 'allow';
  if (paint === 'DENY') return 'deny';
  return 'pending';
}

export function paintLabel(paint, verdict) {
  if (paint === 'UNAVAILABLE') return 'UNAVAILABLE';
  if (paint === 'DENY') return String(verdict || 'DENY');
  return 'ALLOW';
}

export async function loadPublishedCycle(fetchImpl = fetch) {
  try {
    const response = await fetchImpl(CYCLE_PATH, { method: 'GET', cache: 'no-store' });
    if (!response.ok) return null;
    const body = await response.json();
    if (!body || typeof body !== 'object') return null;
    if (body.schema !== CYCLE_SCHEMA) return null;
    return body;
  } catch {
    return null;
  }
}
