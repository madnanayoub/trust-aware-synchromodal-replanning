// Scaling benchmark: consortia of increasing size file reports in a morning burst; the LSP posts claims, sensor
// oracles post records, the port authority posts the outcome and the day is scored.
//   npx hardhat run scripts/bench.js --network besu          -> results/bench_besu.json
// Settings (environment variables): BENCH_STAKEHOLDERS (default "10,100,1000"), BENCH_DAYS (3),
// BENCH_REPORT_RATE (reports per stakeholder per day, 0.3), BENCH_SCORE_BATCH (100), BENCH_CLAIM_BATCH (100), BENCH_OUTCOME_BATCH (100),
// RPC_CONCURRENCY (maximum simultaneous RPC requests, 8).
const fs = require("fs");
const path = require("path");
const { ethers, network } = require("hardhat");
const L = require("./lib");

const SIZES = (process.env.BENCH_STAKEHOLDERS || "10,100,1000").split(",").map(Number);
const DAYS = Number(process.env.BENCH_DAYS || 3);
const RATE = Number(process.env.BENCH_REPORT_RATE || 0.3);
const SCORE_BATCH = Number(process.env.BENCH_SCORE_BATCH || 100);
const CLAIM_BATCH = Number(process.env.BENCH_CLAIM_BATCH || 100);
const OUTCOME_BATCH = Number(process.env.BENCH_OUTCOME_BATCH || 100);
const FIRST_INDEX = 40;           // HD index of the first benchmark stakeholder (accounts 0..39 are the consortium)

function rng(seed) {                // small deterministic generator (mulberry32)
  return () => {
    seed |= 0; seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const RPC_CONCURRENCY = Number(process.env.RPC_CONCURRENCY || 8);

/** Runs at most RPC_CONCURRENCY tasks at a time. */
function limiter(n) {
  let active = 0;
  const queue = [];
  const next = () => {
    if (active >= n || !queue.length) return;
    active++;
    const { fn, res, rej } = queue.shift();
    fn().then(res, rej).finally(() => { active--; next(); });
  };
  return (fn) => new Promise((res, rej) => { queue.push({ fn, res, rej }); next(); });
}
const limit = limiter(RPC_CONCURRENCY);

/** JSON-RPC call that retries (up to 20 times, 0.5 s apart) when the node fails or returns null under load. */
async function rpc(method, params) {
  for (let k = 1; ; k++) {
    try {
      const r = await ethers.provider.send(method, params);
      if (r !== null && r !== undefined) return r;
      if (k >= 20) throw new Error(`${method} returned null ${k} times`);
    } catch (e) {
      if (k >= 20) throw e;
    }
    await new Promise((res) => setTimeout(res, 500));
  }
}

/** Polls new blocks every 250 ms and records when each transaction hash first appears in a block. */
async function watchInclusion(hashes, fromBlock, timeoutMs = 600000) {
  const want = new Set(hashes);
  const seen = {};
  let next = fromBlock + 1;
  const t0 = Date.now();
  while (Object.keys(seen).length < want.size) {
    if (Date.now() - t0 > timeoutMs) throw new Error(`${want.size - Object.keys(seen).length} reports not included after ${timeoutMs / 1000} s`);
    const head = Number(await rpc("eth_blockNumber", []));
    for (; next <= head; next++) {
      const b = await rpc("eth_getBlockByNumber", ["0x" + next.toString(16), false]);
      const now = Date.now();
      for (const h of b.transactions) if (want.has(h) && !seen[h]) seen[h] = { t: now, block: next };
    }
    if (Object.keys(seen).length < want.size) await new Promise((r) => setTimeout(r, 250));
  }
  return seen;
}

/** Benchmark stakeholders: HD wallets that sign their reports locally. Nonces are read once and then tracked here,
 *  so a report needs exactly one RPC call (eth_sendRawTransaction). */
async function wallets(n) {
  const cfg = require("../hardhat.config.js");
  const phrase = cfg.networks.besu.accounts.mnemonic;
  const out = [];
  for (let i = 0; i < n; i++) {
    const w = ethers.HDNodeWallet.fromPhrase(phrase, undefined, `m/44'/60'/0'/0/${FIRST_INDEX + i}`);
    if (network.name === "hardhat") await ethers.provider.send("hardhat_setBalance", [w.address, "0x56bc75e2d63100000"]);
    out.push({ wallet: w, address: w.address, nonce: 0 });
  }
  await Promise.all(out.map((s) => limit(async () => {
    s.nonce = Number(await rpc("eth_getTransactionCount", [s.address, "pending"]));
  })));
  return out;
}

/** Signs a legacy transaction locally and submits it; a resend of an already known transaction counts as sent. */
async function sendSigned(s, to, data, chainId) {
  const raw = await s.wallet.signTransaction({ to, data, nonce: s.nonce, gasLimit: 300_000n, gasPrice: 1_000_000_000n,
                                               chainId, type: 0 });
  const hash = ethers.keccak256(raw);
  for (let k = 1; ; k++) {
    try {
      await ethers.provider.send("eth_sendRawTransaction", [raw]);
      break;
    } catch (e) {
      const m = String(e.message || e).toLowerCase();
      if (m.includes("known") || m.includes("already")) break;
      if (k >= 20) throw e;
      await new Promise((res) => setTimeout(res, 500));
    }
  }
  s.nonce += 1;
  return hash;
}

async function runSize(S) {
  const R = rng(S);
  const signers = (await ethers.getSigners()).map((s) => new ethers.NonceManager(s));
  const [admin, lsp, port, ...rest] = signers;
  const oracles = rest.slice(0, 4);
  const st = await wallets(S);
  const log = [];
  const F = await ethers.getContractFactory("TrustLedger", admin);
  const c = await F.deploy(L.fx(3), L.fx(1));
  await L.receipt(c.deploymentTransaction());
  // membership in chunks (one stakeholder owns one asset A<i>)
  const addrs = st.map((w) => w.address);
  const cAddr = await c.getAddress();
  const chainId = (await ethers.provider.getNetwork()).chainId;
  await L.timed("registerMembers", () => c.connect(admin).registerMembers(
    [lsp, port, ...oracles].map((s) => s.signer.address), [3, 4, 2, 2, 2, 2]), log);
  for (let i = 0; i < S; i += 250) {
    const a = addrs.slice(i, i + 250);
    await L.timed("registerMembers", () => c.connect(admin).registerMembers(a, a.map(() => 1)), log);
    await L.timed("setOwners", () => c.connect(admin).setOwners(a.map((_, j) => L.b32(`A${i + j}`)), a), log);
  }
  const days = [];
  for (let d = 1; d <= DAYS; d++) {
    const nRep = Math.max(1, Math.round(S * RATE));
    const who = [...Array(S).keys()].sort(() => R() - 0.5).slice(0, nRep);
    const b0 = await ethers.provider.getBlockNumber();
    // morning burst: every selected stakeholder files one report at the same moment. At most RPC_CONCURRENCY
    // requests are in flight (Besu limits concurrent RPC connections); inclusion is detected by watching new blocks.
    const t0 = Date.now();
    const sent = await Promise.all(who.map((i) => limit(async () => {
      const ts = Date.now();
      const hash = await sendSigned(st[i], cAddr, c.interface.encodeFunctionData("submitReport", [d, ethers.id(`d${d}-s${i}`)]), chainId);
      return { hash, sent: ts };
    })));
    const included = await watchInclusion(sent.map((x) => x.hash), b0);
    const burstMs = Math.max(...sent.map((x) => included[x.hash].t)) - t0;
    const sub = await Promise.all(sent.map((x) => limit(async () => {
      const r = await L.receipt({ hash: x.hash });
      const row = { label: "submitReport", latency_ms: included[x.hash].t - x.sent, gas: Number(r.gasUsed), block: r.blockNumber };
      log.push(row);
      return { ...row, receipt: r };
    })));
    const burstBlocks = new Set(sub.map((r) => r.block)).size;
    const ids = sub.map((r) => r.receipt.logs.map((l) => c.interface.parseLog(l)).find((x) => x.name === "ReportSubmitted").args.id);
    // sensor records: each oracle posts one batch covering about 5% of the assets
    await Promise.all(oracles.map((o, k) => {
      const m = Math.max(1, Math.round(S * 0.05));
      const tg = Array.from({ length: m }, () => Math.floor(R() * S));
      return L.timed("postSensorRecords", () => c.connect(o).postSensorRecords(d, tg.map(() => 1 + (k % 3)),
        tg.map((i) => L.b32(`A${i}`)), tg.map(() => L.fx(1))), log);
    }));
    // interpreted claims in batches; 70% of the claims are real, 30% false alarms; 10% of assets have omitted events
    const kinds = who.map(() => 1 + Math.floor(R() * 3));
    const mags = who.map(() => L.fx(0.5 + Math.floor(R() * 4) * 0.5));
    for (let i = 0; i < ids.length; i += CLAIM_BATCH) {
      const sl = (x) => x.slice(i, i + CLAIM_BATCH);
      await L.timed("postClaims", () => c.connect(lsp).postClaims(sl(ids), sl(kinds),
        sl(who).map((j) => L.b32(`A${j}`)), sl(mags)), log);
    }
    await L.timed("commitPlan", () => c.connect(lsp).commitPlan(d, ethers.id(`plan${d}`), ethers.id(`expl${d}`)), log);
    const ev = [];
    who.forEach((j, n) => { if (R() < 0.7) ev.push([kinds[n], `A${j}`, 1]); });
    for (let j = 0; j < S; j++) if (!who.includes(j) && R() < 0.1) ev.push([1, `A${j}`, 1]);
    let outcomeGas = 0;
    for (let i = 0; i < ev.length || i === 0; i += OUTCOME_BATCH) {
      const part = ev.slice(i, i + OUTCOME_BATCH);
      const r = await L.timed("postOutcome", () => c.connect(port).postOutcome(d, part.map((e) => e[0]),
        part.map((e) => L.b32(e[1])), part.map((e) => L.fx(e[2])), i + OUTCOME_BATCH >= ev.length), log);
      outcomeGas += r.gas;
    }
    let closed = false, scoreGas = 0, scoreTx = 0;
    const ts = Date.now();
    while (!closed) {
      const r = await L.timed("scoreDay", () => c.connect(lsp).scoreDay(d, SCORE_BATCH), log);
      scoreGas += r.gas; scoreTx += 1;
      closed = r.receipt.logs.some((l) => c.interface.parseLog(l).name === "DayScored");
    }
    const b1 = await ethers.provider.getBlockNumber();
    const lat = sub.map((r) => r.latency_ms).sort((a, b) => a - b);
    days.push({ day: d, reports: nRep, outcome_events: ev.length, burst_ms: burstMs,
                burst_tps: +(nRep / (burstMs / 1000)).toFixed(1), burst_blocks: burstBlocks,
                report_latency_ms_p50: lat[Math.floor(lat.length / 2)], report_latency_ms_p95: lat[Math.floor(lat.length * 0.95)],
                report_latency_ms_max: lat[lat.length - 1], outcome_gas: outcomeGas, score_gas: scoreGas, score_tx: scoreTx,
                score_gas_per_report: Math.round(scoreGas / nRep), score_ms: Date.now() - ts, blocks: b1 - b0 });
    console.log(`S=${S} day ${d}: ${nRep} reports included in ${burstMs} ms over ${burstBlocks} block(s), ` +
      `p95 latency ${days.at(-1).report_latency_ms_p95} ms, scoring ${scoreGas} gas in ${scoreTx} tx`);
  }
  return { stakeholders: S, days, by_operation: L.summarise(log) };
}

async function main() {
  const client = await ethers.provider.send("web3_clientVersion", []);
  const out = { network: network.name, client, date: new Date().toISOString(),
                settings: { sizes: SIZES, days: DAYS, rate: RATE, score_batch: SCORE_BATCH, claim_batch: CLAIM_BATCH, outcome_batch: OUTCOME_BATCH },
                results: [] };
  for (const S of SIZES) out.results.push(await runSize(S));
  fs.mkdirSync(path.join(__dirname, "..", "results"), { recursive: true });
  const f = path.join(__dirname, "..", "results", `bench_${network.name}.json`);
  fs.writeFileSync(f, JSON.stringify(out, null, 1));
  console.log(`written ${f}`);
}

main().catch((e) => { console.error(e); process.exitCode = 1; });
