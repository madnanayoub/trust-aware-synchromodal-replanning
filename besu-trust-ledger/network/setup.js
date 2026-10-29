#!/usr/bin/env node
/*
 * Generates a 4-validator Hyperledger Besu QBFT network that runs in Docker.
 *
 *   node network/setup.js            create keys, genesis and docker-compose.yml (once)
 *   node network/setup.js --force    delete the generated files and create a new chain
 *
 * Output (all in network/): accounts.json (mnemonic of the experiment accounts), qbftConfigFile.json (genesis
 * parameters), genesis.json (with the QBFT extra data naming the 4 validators), nodes/node1..4/key (validator keys),
 * docker-compose.yml.
 * Set BESU_IMAGE to use another Besu image, e.g.  BESU_IMAGE=hyperledger/besu:26.9.0
 */
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");   // only used to check that Docker is running
const { ethers } = require("ethers");

const DIR = __dirname;
const IMAGE = process.env.BESU_IMAGE || "hyperledger/besu:26.9.0";
const N_NODES = 4;
const SUBNET = "172.16.240";
const BLOCK_PERIOD = Number(process.env.BLOCK_PERIOD || 2);
const N_FUNDED = Number(process.env.N_FUNDED || 1100);   // admin, LSP, port, oracles, stakeholders, benchmark accounts

function main() {
  const force = process.argv.includes("--force");
  const generated = ["networkFiles", "nodes", "genesis.json", "docker-compose.yml", "qbftConfigFile.json"];
  if (fs.existsSync(path.join(DIR, "docker-compose.yml")) && !force) {
    console.log("The network is already set up (network/docker-compose.yml exists).\n" +
      "Use --force to create a new chain (run `docker compose down -v` in network/ first).");
    process.exit(1);
  }
  // remove complete or partial output of earlier runs
  for (const g of generated) fs.rmSync(path.join(DIR, g), { recursive: true, force: true });
  for (const g of fs.readdirSync(DIR)) if (g.startsWith("gen-")) fs.rmSync(path.join(DIR, g), { recursive: true, force: true });  // from older versions
  try { execFileSync("docker", ["version", "--format", "{{.Server.Version}}"], { stdio: "pipe" }); }
  catch { console.error("Docker is not running. Start Docker Desktop and try again."); process.exit(1); }

  // 1. experiment accounts (admin, LSP, port authority, oracles, stakeholders) from one mnemonic
  const accPath = path.join(DIR, "accounts.json");
  if (!fs.existsSync(accPath)) {
    const phrase = ethers.Wallet.createRandom().mnemonic.phrase;
    fs.writeFileSync(accPath, JSON.stringify({ mnemonic: phrase, note: "Test accounts of the private network only." }, null, 2));
  }
  const { mnemonic } = JSON.parse(fs.readFileSync(accPath, "utf8"));
  const alloc = {};
  for (let i = 0; i < N_FUNDED; i++) {
    const w = ethers.HDNodeWallet.fromPhrase(mnemonic, undefined, `m/44'/60'/0'/0/${i}`);
    alloc[w.address.toLowerCase()] = { balance: "0xad78ebc5ac6200000" };
  }

  // 2. QBFT configuration: 2 s blocks, high block gas limit; all experiment accounts funded in the genesis block
  const cfg = {
    genesis: {
      config: {
        chainId: 1337,
        berlinBlock: 0,
        qbft: { blockperiodseconds: BLOCK_PERIOD, epochlength: 30000, requesttimeoutseconds: 2 * BLOCK_PERIOD },
      },
      nonce: "0x0",
      timestamp: "0x58ee40ba",
      gasLimit: "0x1fffffffffffff",
      difficulty: "0x1",
      mixHash: "0x63746963616c2062797a616e74696e65206661756c7420746f6c6572616e6365",
      coinbase: "0x0000000000000000000000000000000000000000",
      alloc,
    },
  };
  fs.writeFileSync(path.join(DIR, "qbftConfigFile.json"), JSON.stringify(cfg, null, 2));

  // 3. validator keys (secp256k1, the key format Besu reads with --node-private-key-file) and the QBFT genesis.
  //    QBFT extra data = RLP([32-byte vanity, [validator addresses], no vote, round 0, no seals]).
  const validators = Array.from({ length: N_NODES }, () => ethers.Wallet.createRandom())
    .sort((a, b) => (a.address.toLowerCase() < b.address.toLowerCase() ? -1 : 1));
  const extraData = ethers.encodeRlp([ethers.zeroPadValue("0x", 32), validators.map((w) => w.address.toLowerCase()),
    [], "0x", []]);
  const genesis = { ...cfg.genesis, extraData };
  fs.writeFileSync(path.join(DIR, "genesis.json"), JSON.stringify(genesis, null, 2));
  const pubs = [];
  validators.forEach((w, i) => {
    const nd = path.join(DIR, "nodes", `node${i + 1}`);
    fs.mkdirSync(nd, { recursive: true });
    fs.writeFileSync(path.join(nd, "key"), w.privateKey);
    const pub = w.signingKey.publicKey.slice(4);           // 64-byte public key without the 0x04 prefix
    fs.writeFileSync(path.join(nd, "key.pub"), "0x" + pub);
    fs.writeFileSync(path.join(nd, "validator-address.txt"), w.address.toLowerCase() + "\n");
    pubs.push(pub);
  });
  const keyDirs = validators.map((w) => w.address.toLowerCase());

  // 4. docker-compose.yml: node1 is the bootnode; JSON-RPC of node1..4 on host ports 8545..8548
  const boot = `enode://${pubs[0]}@${SUBNET}.11:30303`;
  const svc = [];
  for (let i = 1; i <= N_NODES; i++) {
    const flags = [
      "--data-path=/data", "--genesis-file=/config/genesis.json", "--node-private-key-file=/config/key",
      `--p2p-host=${SUBNET}.1${i}`, "--p2p-port=30303", "--nat-method=NONE", "--sync-mode=FULL",
      "--rpc-http-enabled", "--rpc-http-host=0.0.0.0", "--rpc-http-port=8545",
      "--rpc-http-api=ETH,NET,QBFT,WEB3,TXPOOL,ADMIN", "--rpc-http-cors-origins=all", "--host-allowlist=*",
      "--revert-reason-enabled", "--logging=INFO",
    ];
    if (i > 1) flags.push(`--bootnodes=${boot}`);
    svc.push(`  node${i}:
    image: ${IMAGE}
    container_name: besu-node${i}
    user: root
    command:
${flags.map((f) => `      - "${f}"`).join("\n")}
    volumes:
      - ./genesis.json:/config/genesis.json:ro
      - ./nodes/node${i}/key:/config/key:ro
      - node${i}-data:/data
    ports:
      - "${8544 + i}:8545"
    networks:
      besu:
        ipv4_address: ${SUBNET}.1${i}
    restart: unless-stopped${i > 1 ? "\n    depends_on:\n      - node1" : ""}`);
  }
  const compose = `# Generated by network/setup.js - 4-validator Hyperledger Besu QBFT network
services:
${svc.join("\n")}

volumes:
${Array.from({ length: N_NODES }, (_, i) => `  node${i + 1}-data:`).join("\n")}

networks:
  besu:
    ipam:
      config:
        - subnet: ${SUBNET}.0/24
`;
  fs.writeFileSync(path.join(DIR, "docker-compose.yml"), compose);
  console.log(`\nNetwork files written to ${DIR}.\nValidators: ${keyDirs.join(", ")}\nNext: cd network && docker compose up -d`);
}

main();
