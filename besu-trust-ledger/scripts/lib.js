// Shared helpers: encoding, consortium set-up, replay of exported traces and reading reputation back.
const fs = require("fs");
const path = require("path");
const { ethers } = require("hardhat");

const KIND = { release: 1, lcs_delay: 2, volume: 3 };
const KIND_NAMES = ["", "release", "lcs_delay", "volume"];
const ONE = 1e6;
const ORACLES = ["AIS_oracle", "rail_signalling_oracle", "terminal_gate_oracle", "weighbridge_oracle"];
const ROLE = { STAKEHOLDER: 1, ORACLE: 2, LSP: 3, PORT_AUTHORITY: 4 };

const b32 = (s) => ethers.encodeBytes32String(s);
const fx = (x) => BigInt(Math.round(x * ONE));
const unfx = (v) => Number(v) / ONE;
const hex32 = (h) => (h.startsWith("0x") ? h : "0x" + h);

function loadTraces(dir = path.join(__dirname, "..", "traces")) {
  return fs.readdirSync(dir).filter((f) => f.endsWith(".json")).sort()
    .map((f) => ({ file: f, ...JSON.parse(fs.readFileSync(path.join(dir, f), "utf8")) }));
}

/** Signers: 0 admin, 1 LSP, 2 port authority, 3-6 sensor oracles, 7.. stakeholders in trace order. */
async function consortium(trace) {
  const s = (await ethers.getSigners()).map((x) => new ethers.NonceManager(x));
  const who = { admin: s[0], lsp: s[1], port: s[2], oracle: {}, stakeholder: {} };
  ORACLES.forEach((o, i) => (who.oracle[o] = s[3 + i]));
  trace.stakeholders.forEach((st, i) => (who.stakeholder[st.id] = s[7 + i]));
  return who;
}

async function deploy(trace, admin) {
  const [a, b] = trace.prior;
  const F = await ethers.getContractFactory("TrustLedger", admin);
  const c = await F.deploy(fx(a), fx(b));
  const rcpt = await c.deploymentTransaction().wait();
  return { contract: c, gasUsed: Number(rcpt.gasUsed) };
}

/** Send a transaction and record submit-to-receipt latency, gas and block. */
let POLL_MS = Number(process.env.RECEIPT_POLL_MS || 250);

/** Wait for a receipt by polling every POLL_MS (the default ethers polling interval of 4 s would distort latency). */
async function receipt(tx) {
  for (;;) {
    let r = null;
    try { r = await ethers.provider.getTransactionReceipt(tx.hash); } catch (e) { /* transient RPC error: poll again */ }
    if (r) {
      if (r.status !== 1) throw new Error(`transaction ${tx.hash} reverted`);
      return r;
    }
    await new Promise((res) => setTimeout(res, POLL_MS));
  }
}

async function timed(label, fn, log) {
  const t0 = Date.now();
  const tx = await fn();
  const r = await receipt(tx);
  const row = { label, latency_ms: Date.now() - t0, gas: Number(r.gasUsed), block: r.blockNumber, receipt: r };
  if (log) log.push({ label, latency_ms: row.latency_ms, gas: row.gas, block: row.block });
  return row;
}

async function register(c, who, trace, log) {
  const members = [], roles = [];
  members.push(await who.lsp.getAddress()); roles.push(ROLE.LSP);
  members.push(await who.port.getAddress()); roles.push(ROLE.PORT_AUTHORITY);
  for (const o of ORACLES) { members.push(await who.oracle[o].getAddress()); roles.push(ROLE.ORACLE); }
  const assets = [], owners = [];
  for (const st of trace.stakeholders) {
    const a = await who.stakeholder[st.id].getAddress();
    members.push(a); roles.push(ROLE.STAKEHOLDER);
    for (const asset of st.assets) { assets.push(b32(asset)); owners.push(a); }
  }
  await timed("registerMembers", () => c.connect(who.admin).registerMembers(members, roles), log);
  await timed("setOwners", () => c.connect(who.admin).setOwners(assets, owners), log);
}

/** Replay one day in four phases, as the framework runs it: morning reports and sensor records (in parallel),
 *  interpretation and plan commitment, end-of-day outcome, scoring. */
async function replayDay(c, who, day, log, scoreBatch = 50) {
  const d = day.day;
  // phase 1: reports (each signed by its sender) and sensor records (one batch per oracle)
  // different senders in parallel; reports of the same sender one after another (keeps nonces in order)
  const res1 = new Array(day.reports.length);
  const bySender = {};
  day.reports.forEach((r, i) => (bySender[r.sender] = bySender[r.sender] || []).push(i));
  const p1 = Object.entries(bySender).map(async ([sender, idx]) => {
    for (const i of idx) {
      res1[i] = await timed("submitReport", () => c.connect(who.stakeholder[sender])
        .submitReport(d, hex32(day.reports[i].text_hash)), log);
    }
  });
  const byOracle = {};
  for (const e of day.evidence) (byOracle[e.oracle] = byOracle[e.oracle] || []).push(e);
  for (const [o, es] of Object.entries(byOracle)) {
    p1.push(timed("postSensorRecords", () => c.connect(who.oracle[o]).postSensorRecords(
      d, es.map((e) => KIND[e.kind]), es.map((e) => b32(e.target)), es.map((e) => fx(e.magnitude))), log));
  }
  await Promise.all(p1);
  // report ids from the ReportSubmitted events, in submission order
  const ids = [];
  for (let i = 0; i < day.reports.length; i++) {
    const ev = res1[i].receipt.logs.map((l) => { try { return c.interface.parseLog(l); } catch { return null; } })
      .find((x) => x && x.name === "ReportSubmitted");
    ids.push(ev.args.id);
  }
  // phase 2: interpreted claims and plan commitment by the LSP (same sender, so sequential)
  if (ids.length) {
    const cl = day.reports.map((r) => r.claim);
    await timed("postClaims", () => c.connect(who.lsp).postClaims(ids,
      cl.map((x) => (x ? KIND[x.kind] : 0)), cl.map((x) => (x ? b32(x.target) : ethers.ZeroHash)),
      cl.map((x) => (x && x.magnitude ? fx(x.magnitude) : 0n))), log);
  }
  await timed("commitPlan", () => c.connect(who.lsp).commitPlan(d, hex32(day.plan_hash), hex32(day.explanation_hash),
    ), log);
  // phase 3: realised outcome by the port authority
  await timed("postOutcome", () => c.connect(who.port).postOutcome(d, day.outcome.map((e) => KIND[e[0]]),
    day.outcome.map((e) => b32(e[1])), day.outcome.map((e) => fx(e[2])), true), log);
  // phase 4: scoring (any member may call it)
  let closed = false;
  while (!closed) {
    const r = await timed("scoreDay", () => c.connect(who.lsp).scoreDay(d, scoreBatch), log);
    closed = r.receipt.logs.some((l) => { try { return c.interface.parseLog(l).name === "DayScored"; } catch { return false; } });
  }
}

async function readReputation(c, address) {
  const r = await c.reputation(address);
  const per = (arr) => ({ release: unfx(arr[1]), lcs_delay: unfx(arr[2]), volume: unfx(arr[3]) });
  return { real: Number(r.real), false: Number(r.falseClaims), omissions: Number(r.omissions), p_real: unfx(r.p),
           p_kind: per(r.pk), q_kind: per(r.qk), factor: per(r.fk) };
}

/** Compare the on-chain state with the reference state; returns a list of mismatches. */
function compare(chain, ref, tol = 1e-5) {
  const bad = [];
  for (const k of ["real", "false", "omissions"]) if (chain[k] !== ref[k]) bad.push(`${k}: ${chain[k]} vs ${ref[k]}`);
  if (Math.abs(chain.p_real - ref.p_real) > tol) bad.push(`p_real: ${chain.p_real} vs ${ref.p_real}`);
  for (const f of ["p_kind", "q_kind", "factor"])
    for (const k of KIND_NAMES.slice(1))
      if (Math.abs(chain[f][k] - ref[f][k]) > tol) bad.push(`${f}.${k}: ${chain[f][k]} vs ${ref[f][k]}`);
  return bad;
}

function summarise(log) {
  const by = {};
  for (const r of log) (by[r.label] = by[r.label] || []).push(r);
  const q = (a, p) => a[Math.min(a.length - 1, Math.floor(p * a.length))];
  const out = {};
  for (const [k, rows] of Object.entries(by)) {
    const lat = rows.map((r) => r.latency_ms).sort((a, b) => a - b);
    const gas = rows.map((r) => r.gas);
    out[k] = { n: rows.length, gas_mean: Math.round(gas.reduce((a, b) => a + b, 0) / gas.length), gas_max: Math.max(...gas),
               latency_ms_p50: q(lat, 0.5), latency_ms_p95: q(lat, 0.95), latency_ms_max: lat[lat.length - 1] };
  }
  return out;
}

module.exports = { receipt, KIND, KIND_NAMES, ROLE, ORACLES, b32, fx, unfx, hex32, loadTraces, consortium, deploy, timed,
                   register, replayDay, readReputation, compare, summarise };
