// Collects the results of replay.js, bench.js and probe.js into results/summary.md and results/summary.json.
//   node scripts/summarise.js [network]        (default network name: besu)
const fs = require("fs");
const path = require("path");

const NET = process.argv[2] || "besu";
const R = path.join(__dirname, "..", "results");
const read = (f) => (fs.existsSync(path.join(R, f)) ? JSON.parse(fs.readFileSync(path.join(R, f), "utf8")) : null);
const md = [];
const sum = { network: NET };

const replay = read(`replay_${NET}.json`);
if (replay) {
  md.push(`## Replay of the framework's ledger traces (${replay.client})`, "",
    "| Trace | Days | Reports | Transactions | Blocks | Equivalent to reference | Mean time per day (s) | Gas per day |",
    "|---|---|---|---|---|---|---|---|");
  for (const t of replay.traces) {
    md.push(`| ρ = ${t.rho.toFixed(2)}, seed ${t.seed} | ${t.days} | ${t.reports} | ${t.transactions} | ${t.blocks} | ` +
      `${t.equivalent ? "yes, every day" : "NO"} | ${(t.day_ms_mean / 1000).toFixed(1)} | ${Math.round(t.gas_total / t.days).toLocaleString("en")} |`);
  }
  const ops = {};
  for (const t of replay.traces) for (const [k, v] of Object.entries(t.by_operation)) (ops[k] = ops[k] || []).push(v);
  md.push("", "| Operation | Transactions | Mean gas | Latency p50 (ms) | Latency p95 (ms) |", "|---|---|---|---|---|");
  for (const [k, vs] of Object.entries(ops)) {
    const n = vs.reduce((a, v) => a + v.n, 0);
    const g = Math.round(vs.reduce((a, v) => a + v.gas_mean * v.n, 0) / n);
    md.push(`| ${k} | ${n} | ${g.toLocaleString("en")} | ${Math.max(...vs.map((v) => v.latency_ms_p50))} | ${Math.max(...vs.map((v) => v.latency_ms_p95))} |`);
  }
  sum.replay = replay.traces.map(({ tx_log, ...t }) => t);
  md.push("");
}

const bench = read(`bench_${NET}.json`);
if (bench) {
  md.push(`## Scaling benchmark (${bench.client})`, "",
    "| Stakeholders | Reports per day | Blocks to include the burst | Report latency p50 / p95 (ms) | Scoring gas per report | Scoring transactions per day | Blocks per day |",
    "|---|---|---|---|---|---|---|");
  for (const r of bench.results) {
    const avg = (f) => r.days.reduce((a, d) => a + d[f], 0) / r.days.length;
    md.push(`| ${r.stakeholders} | ${r.days[0].reports} | ${r.days[0].burst_blocks === undefined ? "–" : avg("burst_blocks").toFixed(1)} | ${Math.round(avg("report_latency_ms_p50"))} / ` +
      `${Math.round(avg("report_latency_ms_p95"))} | ${Math.round(avg("score_gas_per_report")).toLocaleString("en")} | ` +
      `${avg("score_tx").toFixed(1)} | ${avg("blocks").toFixed(1)} |`);
  }
  sum.bench = bench.results.map((r) => ({ stakeholders: r.stakeholders, days: r.days }));
  md.push("");
}

const probes = fs.existsSync(R) ? fs.readdirSync(R).filter((f) => f.startsWith("probe_")) : [];
if (probes.length) {
  md.push("## Fault tolerance", "", "| Test | Probes sent | Included | Max latency (s) | Longest gap between blocks (s) |", "|---|---|---|---|---|");
  sum.faults = [];
  for (const f of probes) {
    const p = read(f);
    const gaps = p.blocks.slice(1).map((b, i) => b.t_s - p.blocks[i].t_s);
    const g = gaps.length ? Math.max(...gaps) : null;
    md.push(`| ${p.label} | ${p.sent} | ${p.included} | ${(p.latency_ms_max / 1000).toFixed(1)} | ${g === null ? "–" : g.toFixed(1)} |`);
    sum.faults.push({ label: p.label, sent: p.sent, included: p.included, latency_ms_max: p.latency_ms_max, max_block_gap_s: g });
  }
  md.push("");
}

if (!md.length) { console.log(`no results for network "${NET}" in results/`); process.exit(1); }
fs.writeFileSync(path.join(R, "summary.md"), `# Besu QBFT experiment summary\n\n${md.join("\n")}`);
fs.writeFileSync(path.join(R, "summary.json"), JSON.stringify(sum, null, 1));
console.log(md.join("\n"));
console.log(`\nwritten results/summary.md and results/summary.json`);
