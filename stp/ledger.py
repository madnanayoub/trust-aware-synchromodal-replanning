"""Simulated permissioned blockchain (hash-chained ledger) for multi-stakeholder synchromodal coordination.

Design (deliberately simple and reproducible, as is usual for this kind of evaluation):
  * Membership: every stakeholder, oracle node and validator holds an Ed25519 key pair; the public keys are registered
    with the consortium. Entries are signed with the author's private key over the canonical JSON and verified with
    the registered public key, so a signature cannot be produced by anyone but its author (non-repudiation).
    Unregistered or badly signed entries are rejected.
  * Blocks: index, timestamp, previous hash, Merkle root of the entry hashes, validator id, validator
    signature, block hash. Validators (e.g. port authority, terminal operator) take turns (round-robin, PoA-style).
  * Immutability: changing any stored entry changes its hash -> Merkle root -> block hash -> breaks the chain;
    `verify_chain()` reports the first inconsistent block.
Entry types used by the planner:
  report      a stakeholder's disturbance message (raw text + sender), before the planning time
  evidence    an independent sensor record (AIS, rail signalling, terminal gate, weighbridge) from an oracle node
  plan        hash of the committed stage-1 plan and its explanation
  outcome     the realised disturbances of the day, posted by the terminal/port oracle after execution
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _h(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def merkle_root(hashes: List[str]) -> str:
    if not hashes:
        return _h(b"")
    level = list(hashes)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [_h((level[i] + level[i + 1]).encode()) for i in range(0, len(level), 2)]
    return level[0]


@dataclass
class Entry:
    type: str
    author: str
    payload: dict
    signature: str = ""

    def body(self):
        return {"type": self.type, "author": self.author, "payload": self.payload}

    @property
    def hash(self):
        return _h(canonical({**self.body(), "signature": self.signature}))


@dataclass
class Block:
    index: int
    timestamp: float
    prev_hash: str
    entries: List[Entry]
    validator: str
    merkle: str = ""
    validator_sig: str = ""
    hash: str = ""

    def header(self):
        return {"index": self.index, "timestamp": self.timestamp, "prev_hash": self.prev_hash,
                "merkle": self.merkle, "validator": self.validator}


class Ledger:
    def __init__(self, members: List[str], validators: List[str], seed: str = "synchro"):
        # simulated key management: deterministic Ed25519 key pairs (reproducible runs); only public keys are needed
        # to verify, private keys stay with their owners
        self._private: Dict[str, Ed25519PrivateKey] = {
            m: Ed25519PrivateKey.from_private_bytes(hashlib.sha256(f"{seed}:{m}".encode()).digest())
            for m in set(members) | set(validators)}
        self.public = {m: k.public_key() for m, k in self._private.items()}
        self.keys = self.public                     # registry of members (public keys)
        self.validators = list(validators)
        self.chain: List[Block] = []
        self.pending: List[Entry] = []
        self.rejected = 0
        genesis = Block(0, 0.0, "0" * 64, [], validators[0])
        self._seal(genesis)
        self.chain.append(genesis)

    # ---------------- signing
    def sign(self, author: str, entry: Entry) -> Entry:
        """Sign with the author's private key (held by the author)."""
        entry.signature = self._private[author].sign(canonical(entry.body())).hex()
        return entry

    def _verify(self, member: str, sig_hex: str, data: bytes) -> bool:
        pk = self.public.get(member)
        if pk is None:
            return False
        try:
            pk.verify(bytes.fromhex(sig_hex), data)
            return True
        except (InvalidSignature, ValueError):
            return False

    def _valid_sig(self, e: Entry) -> bool:
        return self._verify(e.author, e.signature, canonical(e.body()))

    # ---------------- writing
    def submit(self, type_: str, author: str, payload: dict) -> Optional[Entry]:
        if author not in self.keys:
            self.rejected += 1
            return None
        e = self.sign(author, Entry(type_, author, payload))
        self.pending.append(e)
        return e

    def submit_signed(self, e: Entry) -> bool:
        if not self._valid_sig(e):
            self.rejected += 1
            return False
        self.pending.append(e)
        return True

    def _seal(self, b: Block):
        b.merkle = merkle_root([e.hash for e in b.entries])
        b.validator_sig = self._private[b.validator].sign(canonical(b.header())).hex()
        b.hash = _h(canonical({**b.header(), "validator_sig": b.validator_sig}))

    def commit(self, timestamp: float) -> Optional[Block]:
        if not self.pending:
            return None
        val = self.validators[len(self.chain) % len(self.validators)]
        b = Block(len(self.chain), timestamp, self.chain[-1].hash, self.pending, val)
        self._seal(b)
        self.chain.append(b)
        self.pending = []
        return b

    # ---------------- reading
    def entries(self, type_: Optional[str] = None):
        for b in self.chain:
            for e in b.entries:
                if type_ is None or e.type == type_:
                    yield e

    def verify_chain(self) -> Optional[int]:
        """Return None if the whole chain is consistent, else the index of the first invalid block."""
        for i, b in enumerate(self.chain):
            if any(not self._valid_sig(e) for e in b.entries):
                return i
            if b.merkle != merkle_root([e.hash for e in b.entries]):
                return i
            if not self._verify(b.validator, b.validator_sig, canonical(b.header())):
                return i
            if b.hash != _h(canonical({**b.header(), "validator_sig": b.validator_sig})):
                return i
            if i and b.prev_hash != self.chain[i - 1].hash:
                return i
        return None

    def stats(self) -> dict:
        n_e = sum(len(b.entries) for b in self.chain)
        size = sum(len(canonical({**b.header(), "entries": [e.body() for e in b.entries]})) for b in self.chain)
        t0 = time.perf_counter()
        ok = self.verify_chain() is None
        return {"blocks": len(self.chain), "entries": n_e, "bytes": size, "valid": ok,
                "verify_ms": round(1000 * (time.perf_counter() - t0), 2), "rejected_submissions": self.rejected}
