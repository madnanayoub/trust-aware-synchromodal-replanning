require("@nomicfoundation/hardhat-toolbox");
const fs = require("fs");
const path = require("path");

// Accounts for the Besu network are derived from the mnemonic written by `node network/setup.js`.
const accountsFile = path.join(__dirname, "network", "accounts.json");
const mnemonic = fs.existsSync(accountsFile)
  ? JSON.parse(fs.readFileSync(accountsFile, "utf8")).mnemonic
  : "test test test test test test test test test test test junk";

module.exports = {
  solidity: {
    version: "0.8.28",
    settings: { optimizer: { enabled: true, runs: 200 }, evmVersion: "berlin" },
  },
  networks: {
    hardhat: {
      hardfork: "berlin",
      gasPrice: 1_000_000_000,
      blockGasLimit: 100_000_000,
      accounts: { mnemonic, count: 40 },
    },
    besu: {
      url: process.env.BESU_RPC || "http://127.0.0.1:8545",
      chainId: 1337,
      gasPrice: 1_000_000_000,
      accounts: { mnemonic, count: 40 },
      timeout: 120000,
    },
  },
  mocha: { timeout: 600000 },
};
