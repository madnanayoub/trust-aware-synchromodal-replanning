const { expect } = require("chai");
const { ethers } = require("hardhat");
const L = require("../scripts/lib");

const PRIOR = [3, 1];

async function fixture() {
  const [admin, lsp, port, oracle, alice, bob, outsider] = await ethers.getSigners();
  const c = await (await ethers.getContractFactory("TrustLedger")).deploy(L.fx(PRIOR[0]), L.fx(PRIOR[1]));
  await c.registerMembers([lsp, port, oracle, alice, bob].map((s) => s.address),
    [L.ROLE.LSP, L.ROLE.PORT_AUTHORITY, L.ROLE.ORACLE, L.ROLE.STAKEHOLDER, L.ROLE.STAKEHOLDER]);
  await c.setOwners([L.b32("S1"), L.b32("v0001")], [alice.address, bob.address]);
  return { c, admin, lsp, port, oracle, alice, bob, outsider };
}

const h = (s) => ethers.keccak256(ethers.toUtf8Bytes(s));

describe("TrustLedger: access control and append-only records", () => {
  it("rejects writes from accounts without the right role", async () => {
    const { c, alice, outsider, lsp } = await fixture();
    await expect(c.connect(outsider).submitReport(1, h("x"))).to.be.revertedWithCustomError(c, "NotAuthorised");
    await expect(c.connect(alice).postOutcome(1, [], [], [], true)).to.be.revertedWithCustomError(c, "NotAuthorised");
    await expect(c.connect(lsp).registerMembers([outsider.address], [1])).to.be.revertedWithCustomError(c, "NotAuthorised");
  });

  it("interprets a report only once and accepts an outcome only once", async () => {
    const { c, alice, lsp, port } = await fixture();
    await c.connect(alice).submitReport(1, h("S1 released 1.5 h late"));
    await c.connect(lsp).postClaims([0], [1], [L.b32("S1")], [L.fx(1.5)]);
    await expect(c.connect(lsp).postClaims([0], [1], [L.b32("S1")], [L.fx(0.5)]))
      .to.be.revertedWithCustomError(c, "AlreadyPosted");
    await c.connect(port).postOutcome(1, [1], [L.b32("S1")], [L.fx(1.5)], true);
    await expect(c.connect(port).postOutcome(1, [], [], [], true)).to.be.revertedWithCustomError(c, "AlreadyPosted");
    await expect(c.connect(alice).submitReport(1, h("late edit"))).to.be.revertedWithCustomError(c, "AlreadyPosted");
  });

  it("offers no way to change or delete a recorded report", async () => {
    const { c } = await fixture();
    const writes = c.interface.fragments.filter((f) => f.type === "function" && !["view", "pure"].includes(f.stateMutability))
      .map((f) => f.name).sort();
    expect(writes).to.deep.equal(["commitPlan", "postClaims", "postOutcome", "postSensorRecords", "registerMembers",
      "scoreDay", "setOwners", "submitReport"]);
  });
});

describe("TrustLedger: scoring", () => {
  it("scores real, false and omitted events and the magnitude statistics", async () => {
    const { c, alice, bob, lsp, port } = await fixture();
    // day 1: alice reports S1 2 h late (truth 1 h): real, wrong size; bob raises a false alarm on v0001
    await c.connect(alice).submitReport(1, h("a"));
    await c.connect(bob).submitReport(1, h("b"));
    await c.connect(lsp).postClaims([0, 1], [1, 2], [L.b32("S1"), L.b32("v0001")], [L.fx(2), L.fx(1)]);
    await c.connect(port).postOutcome(1, [1], [L.b32("S1")], [L.fx(1)], true);
    await c.scoreDay(1, 10);
    // day 2: nobody reports; v0001 is really delayed -> omission for bob
    await c.connect(port).postOutcome(2, [2], [L.b32("v0001")], [L.fx(0.5)], true);
    await c.scoreDay(2, 10);
    const a = await L.readReputation(c, alice.address);
    const b = await L.readReputation(c, bob.address);
    expect([a.real, a.false, a.omissions]).to.deep.equal([1, 0, 0]);
    expect([b.real, b.false, b.omissions]).to.deep.equal([0, 1, 1]);
    expect(a.p_real).to.be.closeTo(4 / 5, 1e-6);
    expect(b.p_real).to.be.closeTo(3 / 5, 1e-6);
    expect(a.q_kind.release).to.be.closeTo(1 / 3, 1e-6);          // (1 + 0 accurate) / (2 + 1 real)
    expect(a.factor.release).to.equal(1);                          // fewer than two ratios
    expect(a.p_kind.release).to.be.closeTo((2 * 0.8 + 1) / 3, 1e-6);
  });

  it("de-biases with the median of the last 15 ratios", async () => {
    const { c, alice, lsp, port } = await fixture();
    const ratios = [];
    for (let d = 1; d <= 20; d++) {
      const claimed = 0.5 * (1 + (d % 4));          // 1.0, 1.5, 2.0, 0.5 ...
      await c.connect(alice).submitReport(d, h(`r${d}`));
      await c.connect(lsp).postClaims([d - 1], [1], [L.b32("S1")], [L.fx(claimed)]);
      await c.connect(port).postOutcome(d, [1], [L.b32("S1")], [L.fx(1)], true);
      await c.scoreDay(d, 10);
      ratios.push(1 / claimed);
    }
    const last = ratios.slice(-15).sort((x, y) => x - y);
    const a = await L.readReputation(c, alice.address);
    expect(a.factor.release).to.be.closeTo(last[7], 1e-5);
  });

  it("accepts the outcome in batches and closes the day only with the last batch", async () => {
    const { c, alice, bob, lsp, port } = await fixture();
    await c.connect(alice).submitReport(1, h("a"));
    await c.connect(lsp).postClaims([0], [1], [L.b32("S1")], [L.fx(1)]);
    await c.connect(port).postOutcome(1, [2], [L.b32("v0001")], [L.fx(1)], false);
    await expect(c.connect(bob).submitReport(1, h("too late"))).to.be.revertedWithCustomError(c, "AlreadyPosted");
    await expect(c.scoreDay(1, 10)).to.be.revertedWithCustomError(c, "BadInput");
    await c.connect(port).postOutcome(1, [1], [L.b32("S1")], [L.fx(1)], true);
    await c.scoreDay(1, 10);
    expect((await L.readReputation(c, alice.address)).real).to.equal(1);
    expect((await L.readReputation(c, bob.address)).omissions).to.equal(1);   // v0001 delayed, nobody reported it
  });

  it("scores a large day in several batches", async () => {
    const { c, alice, lsp, port } = await fixture();
    for (let i = 0; i < 7; i++) await c.connect(alice).submitReport(1, h(`r${i}`));
    await c.connect(lsp).postClaims([0, 1, 2, 3, 4, 5, 6], Array(7).fill(1), Array(7).fill(L.b32("S1")), Array(7).fill(L.fx(1)));
    await c.connect(port).postOutcome(1, [1], [L.b32("S1")], [L.fx(1)], true);
    await c.scoreDay(1, 3);
    await c.scoreDay(1, 3);
    expect(await c.dayScored(1)).to.equal(false);
    await c.scoreDay(1, 3);
    expect(await c.dayScored(1)).to.equal(true);
    expect((await L.readReputation(c, alice.address)).real).to.equal(7);
  });
});

describe("TrustLedger: equivalence with the reference implementation (exported traces)", () => {
  for (const trace of L.loadTraces()) {
    it(`${trace.file}: reputation equals stp/trust.py after every day`, async () => {
      const who = await L.consortium(trace);
      const { contract: c } = await L.deploy(trace, who.admin);
      await L.register(c, who, trace);
      for (const day of trace.days) {
        await L.replayDay(c, who, day, null);
        for (const st of trace.stakeholders) {
          const chain = await L.readReputation(c, await who.stakeholder[st.id].getAddress());
          const bad = L.compare(chain, day.reputation_after[st.id]);
          expect(bad, `day ${day.day}, ${st.id}`).to.deep.equal([]);
        }
      }
    });
  }
});
