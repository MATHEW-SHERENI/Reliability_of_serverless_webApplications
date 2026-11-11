// M5 workload generator: the open, arrival-rate workload for one run.
//
// Environment variables (set by the orchestrator, or by hand for a smoke test):
//   BASE_URL   API Gateway URL ending in "/"                      (required)
//   CONDITION  C1..C7, selects the arrival pattern (midterm Table 3)  [C1]
//   CONFIG     Base, Retry, Cache or Retry+Cache, copied into records [unknown]
//   RUN_ID     identifies the run in every record                    [local]
//   SEED       seeds the per-VU random generators                    [5360]
//   WARMUP_S   warm-up seconds before the measured phase   [30; 0 for C2]
//   SCALE      multiplies every rate, e.g. 0.1 for a laptop smoke test [1]
//   MAIN_S     measured seconds of a steady condition                [180]
//   STEP_S     seconds per C3 ramp step                               [60]
//   SUMMARY    path of the end-of-run summary file          [summary.json]
//
// One JSON line per request is written with console.log; run k6 with
// --console-output=<file> --log-format=raw so the file holds only those lines.
import http from 'k6/http';
import exec from 'k6/execution';

const BASE_URL = __ENV.BASE_URL;
const CONDITION = __ENV.CONDITION || 'C1';
const CONFIG = __ENV.CONFIG || 'unknown';
const RUN_ID = __ENV.RUN_ID || 'local';
const SEED = parseInt(__ENV.SEED || '5360', 10);
const WARMUP_S = parseInt(__ENV.WARMUP_S || (CONDITION === 'C2' ? '0' : '30'), 10);
const SCALE = parseFloat(__ENV.SCALE || '1');
const MAIN_S = parseInt(__ENV.MAIN_S || '180', 10);
const STEP_S = parseInt(__ENV.STEP_S || '60', 10);

const PRODUCTS = 1000;
const READ_SHARE = 0.9;
const CLIENT_TIMEOUT = '10s';
const STEADY_RATE = 50;
const RAMP_RATES = [100, 250, 500, 1000, 2000];
const BURST_VUS = 100;

if (!BASE_URL) {
  throw new Error('BASE_URL is required, e.g. https://abc.execute-api.us-east-2.amazonaws.com/');
}

// ---------------------------------------------------------------- scenarios

function rate(r) {
  return Math.max(1, Math.round(r * SCALE));
}

function vus(peakRate) {
  // Enough VUs for every request to take up to 0.5 s at the peak rate, and at
  // least 200 so slow conditions (C6: up to 3 s per request at 50 req/s) never
  // run out. A shortage shows up as dropped iterations, which invalidates the run.
  return { preAllocatedVUs: Math.ceil(peakRate * 0.2) + 20, maxVUs: Math.max(200, Math.ceil(peakRate * 0.5)) };
}

function steady(r, seconds, startS) {
  return Object.assign({
    executor: 'constant-arrival-rate', rate: r, timeUnit: '1s',
    duration: `${seconds}s`, startTime: `${startS}s`,
  }, vus(r));
}

function buildScenarios() {
  const scenarios = {};
  if (CONDITION === 'C2') {
    // 100 simultaneous reads right after a configuration update, then 50 req/s.
    // The steady phase starts after the 10 s client timeout, so the burst is over.
    scenarios.burst = { executor: 'per-vu-iterations', vus: Math.max(1, Math.round(BURST_VUS * SCALE)),
                        iterations: 1, maxDuration: '11s' };
    scenarios.main = steady(rate(STEADY_RATE), 30, 11);
    return scenarios;
  }
  if (CONDITION === 'C3') {
    if (WARMUP_S > 0) scenarios.warmup = steady(rate(RAMP_RATES[0]), WARMUP_S, 0);
    const stages = [];
    for (const r of RAMP_RATES) {
      stages.push({ target: rate(r), duration: '1s' });              // step up
      stages.push({ target: rate(r), duration: `${STEP_S - 1}s` });  // hold
    }
    scenarios.main = Object.assign({
      executor: 'ramping-arrival-rate', startRate: rate(RAMP_RATES[0]), timeUnit: '1s',
      startTime: `${WARMUP_S}s`, stages,
    }, vus(rate(RAMP_RATES[RAMP_RATES.length - 1])));
    return scenarios;
  }
  if (!['C1', 'C4', 'C5', 'C6', 'C7'].includes(CONDITION)) {
    throw new Error(`unknown CONDITION ${CONDITION}`);
  }
  if (WARMUP_S > 0) scenarios.warmup = steady(rate(STEADY_RATE), WARMUP_S, 0);
  scenarios.main = steady(rate(STEADY_RATE), MAIN_S, WARMUP_S);
  return scenarios;
}

export const options = {
  scenarios: buildScenarios(),
  discardResponseBodies: true,   // the headers carry everything the analysis needs
  summaryTrendStats: ['med', 'p(95)', 'p(99)', 'max'],
};

// ------------------------------------------------------------ random choices

// mulberry32: a small seeded generator. Math.random cannot be seeded, and each
// VU needs its own reproducible stream, so the generator is seeded per VU.
function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// Zipf (s = 1) over product IDs 1..1000: P(k) is proportional to 1/k, so
// product 1 is read about twice as often as product 2 and 1,000 times as
// often as product 1000. The cumulative distribution is computed once.
const ZIPF_CDF = (() => {
  const cdf = new Float64Array(PRODUCTS);
  let total = 0;
  for (let k = 1; k <= PRODUCTS; k++) { total += 1 / k; cdf[k - 1] = total; }
  for (let i = 0; i < PRODUCTS; i++) cdf[i] /= total;
  return cdf;
})();

function zipfProduct(u) {
  let lo = 0, hi = PRODUCTS - 1;            // first index with cdf >= u
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (ZIPF_CDF[mid] < u) lo = mid + 1; else hi = mid;
  }
  return lo + 1;
}

let rng = null;                              // one generator per VU, made on first use

// -------------------------------------------------------------- one request

function header(res, name) {
  const value = res.headers[name];
  return value === undefined ? null : value;
}

export default function () {
  if (rng === null) rng = mulberry32(SEED * 1000003 + exec.vu.idInTest);
  const phase = exec.scenario.name;          // warmup, burst or main
  const isRead = phase === 'burst' || rng() < READ_SHARE;

  let res, productId;
  const sentAt = Date.now();
  if (isRead) {
    productId = zipfProduct(rng());
    res = http.get(`${BASE_URL}products/${productId}`,
                   { timeout: CLIENT_TIMEOUT, tags: { name: 'GET /products/{id}' } });
  } else {
    productId = 1 + Math.floor(rng() * PRODUCTS);        // orders: uniform IDs
    const key = `${RUN_ID}-${phase}-${exec.scenario.iterationInTest}`;
    res = http.post(`${BASE_URL}orders`, JSON.stringify({ product_id: productId, quantity: 1 }),
                    { timeout: CLIENT_TIMEOUT, tags: { name: 'POST /orders' },
                      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key } });
  }

  console.log(JSON.stringify({
    t: sentAt,                                   // ms since epoch, when the request was sent
    run: RUN_ID, cond: CONDITION, cfg: CONFIG, ph: phase,
    op: isRead ? 'get' : 'post', id: productId,
    st: res.status,                              // 0 when no response (client timeout, reset)
    lat: Math.round(res.timings.duration * 100) / 100,     // ms: send + wait + receive
    conn: Math.round((res.timings.connecting + res.timings.tls_handshaking) * 100) / 100,
    att: header(res, 'X-Attempts'), cache: header(res, 'X-Cache'),
    cold: header(res, 'X-Cold-Start'), env: header(res, 'X-Env-Id'),
    ver: header(res, 'X-Data-Version'), rid: header(res, 'X-Request-Id'),
    err: res.error_code || 0,
  }));
}

// ------------------------------------------------------------- run summary

export function handleSummary(data) {
  const count = (name) => (data.metrics[name] ? data.metrics[name].values.count : 0);
  const summary = {
    run: RUN_ID, condition: CONDITION, config: CONFIG,
    requests: count('http_reqs'),
    dropped_iterations: count('dropped_iterations'),   // > 0 makes the run invalid
    max_vus: data.metrics.vus_max ? data.metrics.vus_max.values.max : 0,
  };
  return {
    [__ENV.SUMMARY || 'summary.json']: JSON.stringify(summary, null, 2) + '\n',
    stdout: `${JSON.stringify(summary)}\n`,
  };
}
