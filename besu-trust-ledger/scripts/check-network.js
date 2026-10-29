// Network health check: client version, chain id, peers, validators and recent block times.
//   npx hardhat run scripts/check-network.js --network besu
const { ethers, network } = require("hardhat");

async function main() {
  const p = ethers.provider;
  const [ver, chainId, peers, head] = await Promise.all([
    p.send("web3_clientVersion", []), p.send("eth_chainId", []), p.send("net_peerCount", []), p.getBlockNumber()]);
  console.log(`network      ${network.name}\nclient       ${ver}\nchain id     ${Number(chainId)}\npeers        ${Number(peers)}\nblock        ${head}`);
  if (network.name !== "hardhat") {
    const vals = await p.send("qbft_getValidatorsByBlockNumber", ["latest"]);
    console.log(`validators   ${vals.length}: ${vals.join(", ")}`);
  }
  const n = Math.min(10, head);
  if (n > 1) {
    const blocks = await Promise.all(Array.from({ length: n }, (_, i) => p.getBlock(head - i)));
    const gaps = blocks.slice(0, -1).map((b, i) => b.timestamp - blocks[i + 1].timestamp);
    console.log(`block times  ${gaps.join(" ")} s (last ${n} blocks)`);
  }
  const [admin] = await ethers.getSigners();
  console.log(`admin        ${admin.address}, balance ${ethers.formatEther(await p.getBalance(admin.address))} ETH`);
}

main().catch((e) => { console.error(e); process.exitCode = 1; });
