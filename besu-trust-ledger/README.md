# On-chain trust ledger on Hyperledger Besu (QBFT)

This package runs the trust layer of the trust-aware synchromodal replanning framework on a real permissioned blockchain. It contains:

- **`contracts/TrustLedger.sol`**: an append-only consortium ledger and Beta reputation contract.
  - Stakeholders sign their own reports.
  - The LSP posts the interpreted claim of each report, plus the plan and explanation hashes.
  - Sensor oracles post independent records.
  - The port authority posts the realised events of each day.
  - Any member can then score the day.
  - Scoring reproduces `stp/trust.py` exactly; the tests check this day by day on exported traces.
- **`network/setup.js`**: generates a 4-validator Besu QBFT network that runs in Docker.
- **`scripts/`**: replay of the framework's ledger traces, a scaling benchmark, a fault-tolerance probe and a summary script.

Free-text messages, network data and plans stay off-chain. Only their hashes, the structured claims, sensor records, outcomes and reputation counts are on-chain.

```
contracts/TrustLedger.sol     smart contract
test/TrustLedger.test.js      unit tests and equivalence with the reference implementation
traces/                       ledger traces exported from the Python framework (scripts/export_chain_trace.py)
network/setup.js              generates keys, genesis and docker-compose.yml for 4 QBFT validators
scripts/check-network.js      health check: peers, validators, block times
scripts/replay.js             replays every trace on the chain and checks equivalence after every day
scripts/bench.js              scaling benchmark (10 / 100 / 1,000 stakeholders)
scripts/probe.js              fault-tolerance probe (run while stopping validators)
scripts/summarise.js          collects all results into results/summary.md
```

The commands below are for **Windows PowerShell**. For macOS or Linux, the bash form is given where it differs.

---

## 0. Prerequisites (once)

| Software | Version | Check |
|---|---|---|
| Docker Desktop with the WSL 2 backend | current | `docker version` shows a Server section |
| Node.js LTS | 22.x | `node -v` |
| The main repository (Python) | as used for the paper | only needed to export more traces |

In Docker Desktop → Settings → Resources, give Docker at least 4 CPUs and 8 GB of memory.

Ports 8545–8548 must be free on your PC. The Docker subnet 172.16.240.0/24 must not clash with your LAN or VPN.

## 1. Development: install, compile, test (no Besu needed)

```powershell
cd C:\path\to\besu-trust-ledger
npm install
npx hardhat compile
npx hardhat test
```

Expected: `12 passing`. The last five tests replay the exported traces on Hardhat's built-in chain and check that the on-chain reputation equals the Python reference after every day.

Optionally, run the full replay locally:

```powershell
npx hardhat run scripts/replay.js
```

It should print `EQUIVALENT to stp/trust.py on every day` for each trace.

## 2. Create the Besu QBFT network

```powershell
docker pull hyperledger/besu:26.9.0
node network/setup.js
cd network
docker compose up -d
docker compose ps
```

What `setup.js` does:

- It generates `network/accounts.json`, the mnemonic of the experiment accounts. These are test keys for this private network only.
- It funds 1,100 accounts in the genesis block.
- It generates 4 validator keys (secp256k1) and the QBFT genesis, whose extra data names the 4 validators.
- It writes `network/docker-compose.yml`.

If `docker pull` fails because the tag does not exist, look up the current tag on Docker Hub (`hyperledger/besu`). Then run `$env:BESU_IMAGE="hyperledger/besu:<tag>"` before `node network/setup.js`. In bash: `BESU_IMAGE=hyperledger/besu:<tag> node network/setup.js`.

Wait about 30 seconds, then check the network from the package folder:

```powershell
cd ..
docker logs besu-node1 --tail 20
npx hardhat run scripts/check-network.js --network besu
```

Expected:

- `peers 3` and `validators 4`;
- block times of `2 2 2 ...` s;
- an admin balance of 200 ETH.

The logs show `Produced #… / Imported #…` every 2 s.

## 3. Deployment and replay of the framework traces

```powershell
npx hardhat run scripts/replay.js --network besu
```

- For each trace in `traces/`, the script deploys a fresh contract and registers the consortium: LSP, port authority, 4 sensor oracles and 7 stakeholders, with their assets.
- It then replays the 40 days in four phases per day:
  1. reports and sensor records;
  2. claims and plan commitment;
  3. outcome;
  4. scoring.
- After every day it compares the on-chain reputation of every stakeholder with the Python reference.
- One trace takes about 5–8 minutes with 2 s blocks.

Output: `results/replay_besu.json`, with every transaction's gas and latency.

## 4. Scaling benchmark

```powershell
$env:BENCH_STAKEHOLDERS="10,100,1000"; $env:BENCH_DAYS="3"
npx hardhat run scripts/bench.js --network besu
```

In bash: `BENCH_STAKEHOLDERS=10,100,1000 BENCH_DAYS=3 npx hardhat run scripts/bench.js --network besu`

- For each consortium size, 30% of the stakeholders file a report in the same second (the morning burst).
- The LSP posts claims, the oracles post records, the port authority posts the outcome and the day is scored in batches.
- Output: `results/bench_besu.json`.
- Expect about 15–30 minutes in total.

## 5. Fault tolerance

QBFT with 4 validators tolerates 1 faulty validator. With 2 validators down, it stops producing blocks until one returns.

Each test uses two PowerShell windows, both opened in the package folder. Keep `besu-node1` running, because the scripts use its RPC port.

**Test A – one validator down.**

Window 1:

```powershell
$env:PROBE_LABEL="one_down"; $env:PROBE_SECONDS="240"
npx hardhat run scripts/probe.js --network besu
```

Window 2, about 60 s after the probe starts:

```powershell
docker stop besu-node4
```

Then, about 2 minutes later:

```powershell
docker start besu-node4
```

Expected: every probe is included, with a few seconds of extra latency at most.

**Test B – two validators down (loss of quorum).**

Window 1:

```powershell
$env:PROBE_LABEL="two_down"; $env:PROBE_SECONDS="240"
npx hardhat run scripts/probe.js --network besu
```

Window 2, about 60 s after the probe starts:

```powershell
docker stop besu-node3 besu-node4
```

Then, about 90 s later:

```powershell
docker start besu-node3 besu-node4
```

Expected: blocks stop while two validators are down. The probes wait in the transaction pool and are included once the validators return, so no report is lost; their latency equals the outage.

## 6. Resource use

Run these after the replay and the benchmark:

```powershell
docker exec besu-node1 du -sh /data
docker stats --no-stream
```

Note the chain size and the CPU and memory use of each node.

## 7. Summary and results

```powershell
node scripts/summarise.js
```

This writes `results/summary.md` (tables for the paper) and `results/summary.json`.

Send back:

- everything in `results/`;
- the output of step 6;
- the Besu version from `check-network.js`.

## 8. More traces (optional)

The package includes traces for ρ = 0, 0.25, 0.5, 0.75 and 1 with seed 44. To add other seeds, run this in the main repository:

```powershell
python -m scripts.export_chain_trace --rho 0 0.25 0.5 0.75 1 --seeds 55 66 --backend cplex
copy results\chain_traces\*.json C:\path\to\besu-trust-ledger\traces\
```

`replay.js` replays every file in `traces/`.

## 9. Stop and reset

```powershell
cd network
docker compose stop          # stop; the chain is kept
docker compose down -v       # remove containers and chain data
node setup.js --force        # new keys and genesis (a new chain)
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| A container exits with `Unknown option` | That flag is not supported by your Besu version. Remove the line from `network/docker-compose.yml`, run `docker compose up -d` again and report it. |
| `peers 0` or no new blocks | Wait 30–60 s; QBFT needs 3 of 4 validators online. Check `docker logs besu-node2` for errors about the bootnode. |
| `Pool overlaps with other one on this address space` | Change `SUBNET` at the top of `network/setup.js`, then run `node network/setup.js --force`. |
| Port 8545 already in use | Stop the other program (for example Ganache or another node), or change the port mapping in `docker-compose.yml` and set `$env:BESU_RPC="http://127.0.0.1:<port>"`. |
| `docker run` cannot mount the folder | Move the package to a path without spaces, for example `C:\besu-trust-ledger`. |
| Replay is slow | This is normal: each day needs at least four blocks of 2 s. |

## Design notes for the paper

- **Signatures.** Every transaction is signed with the sender's own secp256k1 key (ECDSA, as in Ethereum), which gives non-repudiation. The Python simulation used Ed25519; the security argument is the same.
- **Append-only.** The contract has no function that edits or deletes a report, claim, sensor record, plan hash or outcome. Each report is interpreted once, and no report for a day is accepted after its outcome has started.
- **History-rewriting attack.** The attack of the paper's Section 7.2 cannot be expressed on this chain. Rewriting an earlier record would require a majority of the validators to rebuild every later block.
- **Scoring.** Scoring is permissionless and batched (`scoreDay(day, maxReports)`), so a large day can be split across transactions and no single party can withhold it.
- **Determinism.** Fixed-point arithmetic (1e6 = 1.0) reproduces the Python values to within 1e-5.
