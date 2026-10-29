// Replays every exported trace (traces/*.json) on the selected network, checks after every day that the on-chain
// reputation equals the reference implementation, and records gas and latency of every transaction.
//   npx hardhat run scripts/replay.js --network besu        -> results/replay_besu.json
const fs = require("fs");
const path = require("path");
const { ethers, network } = require("hardhat");
const L = require("./lib");

async function main() {
  const traces = L.loadTraces();
  if (!traces.length) throw new Error("no traces in traces/ - export them with scripts/export_chain_trace.py");
  const client = await ethers.provider.send("web3_clientVersion", []);
  const out = { network: network.name, client, date: new Date().toISOString(), traces: [] };
  for (const trace of traces) {
    const who = await L.consortium(trace);
    const log = [];
    const t0 = Date.now();
    const b0 = await ethers.provider.getBlockNumber();
    const { contract: c, gasUsed } = await L.deploy(trace, who.admin);
    await L.register(c, who, trace, log);
    const mismatches = [];
    const dayTimes = [];
    for (const day of trace.days) {
      const td = Date.now();
      await L.replayDay(c, who, day, log);
      dayTimes.push(Date.now() - td);
      for (const st of trace.stakeholders) {
        const chain = await L.readReputation(c, await who.stakeholder[st.id].getAddress());
        for (const m of L.compare(chain, day.reputation_after[st.id])) mismatches.push(`day ${day.day} ${st.id} ${m}`);
      }
      process.stdout.write(`\r${trace.file}: day ${day.day}/${trace.days.length}`);
    }
    const b1 = await ethers.provider.getBlockNumber();
    const t = {
      file: trace.file, rho: trace.rho, seed: trace.seed, contract: await c.getAddress(), deploy_gas: gasUsed,
      equivalent: mismatches.length === 0, mismatches: mismatches.slice(0, 20),
      days: trace.days.length, reports: trace.days.reduce((a, d) => a + d.reports.length, 0),
      transactions: log.length, blocks: b1 - b0, seconds: (Date.now() - t0) / 1000,
      day_ms_mean: Math.round(dayTimes.reduce((a, b) => a + b, 0) / dayTimes.length), day_ms_max: Math.max(...dayTimes),
      gas_total: log.reduce((a, r) => a + r.gas, 0), by_operation: L.summarise(log), tx_log: log,
    };
    out.traces.push(t);
    console.log(`\n${trace.file}: ${t.equivalent ? "EQUIVALENT to stp/trust.py on every day" : "MISMATCH"}; ` +
      `${t.transactions} tx in ${t.blocks} blocks, ${t.seconds.toFixed(0)} s, day ${t.day_ms_mean} ms on average`);
  }
  fs.mkdirSync(path.join(__dirname, "..", "results"), { recursive: true });
  const f = path.join(__dirname, "..", "results", `replay_${network.name}.json`);
  fs.writeFileSync(f, JSON.stringify(out, null, 1));
  console.log(`written ${f}`);
  if (out.traces.some((t) => !t.equivalent)) process.exitCode = 1;
}

main().catch((e) => { console.error(e); process.exitCode = 1; });
