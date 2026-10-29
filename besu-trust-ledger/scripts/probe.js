// Fault-tolerance probe: submits one report every PROBE_INTERVAL_MS and records whether and when it is included,
// while you stop and restart validator containers in another window.
//   PowerShell:  $env:PROBE_SECONDS=240; npx hardhat run scripts/probe.js --network besu
//   bash:        PROBE_SECONDS=240 npx hardhat run scripts/probe.js --network besu
// Output: results/probe_<label>.json (PROBE_LABEL, default "fault").
const fs = require("fs");
const path = require("path");
const { ethers, network } = require("hardhat");
const L = require("./lib");

const SECONDS = Number(process.env.PROBE_SECONDS || 240);
const INTERVAL = Number(process.env.PROBE_INTERVAL_MS || 3000);
const LABEL = process.env.PROBE_LABEL || "fault";

async function main() {
  const s = await ethers.getSigners();
  const admin = new ethers.NonceManager(s[0]);
  const reporter = new ethers.NonceManager(s[7]);
  const c = await (await ethers.getContractFactory("TrustLedger", admin)).deploy(L.fx(3), L.fx(1));
  await L.receipt(c.deploymentTransaction());
  await L.receipt(await c.connect(admin).registerMembers([s[7].address], [1]));
  console.log(`probe running for ${SECONDS} s; stop/start validators now (docker stop besu-node4 ...)`);
  const rows = [];
  const pending = [];
  const t0 = Date.now();
  let k = 0;
  let lastBlock = await ethers.provider.getBlockNumber();
  const blocks = [];
  while (Date.now() - t0 < SECONDS * 1000) {
    const sent = Date.now();
    const i = k++;
    pending.push(c.connect(reporter).submitReport(1, ethers.id(`probe${i}`))
      .then((tx) => L.receipt(tx))
      .then((r) => rows.push({ i, sent_s: (sent - t0) / 1000, latency_ms: Date.now() - sent, block: r.blockNumber }))
      .catch((e) => rows.push({ i, sent_s: (sent - t0) / 1000, error: String(e.message || e).slice(0, 120) })));
    try {
      const b = await ethers.provider.getBlockNumber();
      if (b !== lastBlock) { blocks.push({ t_s: (Date.now() - t0) / 1000, block: b }); lastBlock = b; }
    } catch (e) { /* node unreachable */ }
    await new Promise((r) => setTimeout(r, INTERVAL));
    const done = rows.length ? rows[rows.length - 1] : null;
    if (done && done.latency_ms) process.stdout.write(`\rt=${((Date.now() - t0) / 1000).toFixed(0)} s  block ${lastBlock}  last latency ${done.latency_ms} ms   `);
  }
  console.log("\nwaiting for outstanding probes (up to 120 s)...");
  await Promise.race([Promise.allSettled(pending), new Promise((r) => setTimeout(r, 120000))]);
  rows.sort((a, b) => a.i - b.i);
  const ok = rows.filter((r) => r.latency_ms);
  const out = { network: network.name, label: LABEL, seconds: SECONDS, interval_ms: INTERVAL, sent: k, included: ok.length,
                latency_ms_max: Math.max(...ok.map((r) => r.latency_ms)), probes: rows, blocks };
  const f = path.join(__dirname, "..", "results", `probe_${LABEL}.json`);
  fs.mkdirSync(path.dirname(f), { recursive: true });
  fs.writeFileSync(f, JSON.stringify(out, null, 1));
  console.log(`${ok.length}/${k} probes included, max latency ${out.latency_ms_max} ms; written ${f}`);
}

main().catch((e) => { console.error(e); process.exitCode = 1; });
