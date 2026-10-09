#!/usr/bin/env python3
"""
besu_inspect.py - diagnostics for the "one tx per block" problem on a
Besu QBFT network, and a shared helper module for zkroam_workload.py.

Subcommands
-----------
  blocks      Table of recent blocks: proposer, tx count, gas, spacing, and a
              round-robin check of the proposer rotation.
  pool        Per-node txpool snapshot: head height, peers, pool size, pending
              txs, nonce-gap (non-executable) check, and optionally which of the
              txs from a detail CSV each node holds.
  leg         Post-mortem of one detail CSV written by zkroam_workload.py:
              for every tx, the entry node, the block that mined it, that
              block's proposer, and whether it matches "only the entry node's
              turn can include it".
  probe       Decisive live experiment: send ONE tx to each chosen node (one
              distinct account per node), check how many nodes hold it after a
              short settle time, then see which node's block includes it.
  validators  QBFT validator set, mapped to topology node names.

All commands read networkFiles/topology.json (override with --topology).
Proposer -> node-name mapping needs each topology node to carry its validator
address under "address" (also tried: validator_address, coinbase, validator).
Without it, addresses are shown raw and hit-rate checks are skipped.

Requirements: requests (always), web3/eth_account (probe only).

Examples
--------
  python besu_inspect.py blocks --last 40
  python besu_inspect.py pool --detail results/zkroam/detail/aggregate_n8_8_500.0_32_16_r2.csv
  python besu_inspect.py leg --detail results/zkroam/detail/aggregate_n8_8_500.0_32_16_r2.csv
  python besu_inspect.py probe --accounts accounts/accounts.json
  python besu_inspect.py probe --accounts accounts/accounts.json --broadcast
"""

import argparse
import csv
import json
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests


# ============================================================
# Basics
# ============================================================

def load_json(path):
    with open(path) as f:
        return json.load(f)


def hx(v):
    """hex string or int -> int (None stays None)."""
    if v is None:
        return None
    if isinstance(v, int):
        return v
    s = str(v)
    return int(s, 16) if s.startswith("0x") else int(s)


def norm_addr(a):
    return str(a).lower().replace("0x", "") if a else ""


def norm_hash(h):
    h = str(h)
    return h if h.startswith("0x") else "0x" + h


def fmt_ts(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")


class RpcError(RuntimeError):
    pass


class Rpc:
    """Minimal JSON-RPC client (thread-safe: one Session per thread)."""

    def __init__(self, url, name="?", timeout=15.0):
        self.url = url
        self.name = name
        self.timeout = timeout
        self._local = threading.local()
        self._id = 0
        self._id_lock = threading.Lock()

    def _session(self):
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            self._local.s = s
        return s

    def call(self, method, params=None):
        with self._id_lock:
            self._id += 1
            rid = self._id
        payload = {"jsonrpc": "2.0", "id": rid, "method": method,
                   "params": params or []}
        r = self._session().post(self.url, json=payload, timeout=self.timeout)
        r.raise_for_status()
        body = r.json()
        err = body.get("error")
        if err:
            raise RpcError(f"{method}: {err.get('message')} (code {err.get('code')})")
        return body.get("result")

    def try_call(self, method, params=None):
        """-> (ok, result_or_error_string)"""
        try:
            return True, self.call(method, params)
        except Exception as e:  # noqa: BLE001
            return False, str(e)[:200]


def build_rpcs(topology, host="localhost", timeout=15.0):
    return [
        Rpc(f"http://{host}:{n['host_rpc_port']}", n["name"], timeout)
        for n in topology["nodes"]
    ]


def rpcs_from_endpoints(endpoints, timeout=15.0):
    """endpoints as built by besu_benchmark.build_rpc_endpoints()."""
    return [Rpc(ep["url"], ep["name"], timeout) for ep in endpoints]


def pick_node(rpcs, spec):
    if spec is None:
        return rpcs[0]
    s = str(spec)
    if s.isdigit() and int(s) < len(rpcs):
        return rpcs[int(s)]
    for r in rpcs:
        if r.name == s:
            return r
    raise SystemExit(f"unknown node {spec!r}; known: {[r.name for r in rpcs]}")


# ============================================================
# Proposer address -> node name
# ============================================================

class NodeDirectory:
    ADDR_KEYS = ("address", "validator_address", "coinbase", "validator")

    def __init__(self, topology, rpcs=None, probe_coinbase=True):
        self.name_by_addr = {}
        self.addr_by_name = {}
        self.node_names = [n["name"] for n in topology["nodes"]]

        for n in topology["nodes"]:
            for k in self.ADDR_KEYS:
                if n.get(k):
                    addr = norm_addr(n[k])
                    self.name_by_addr[addr] = n["name"]
                    self.addr_by_name[n["name"]] = addr
                    break

        # Fallback: ask each node for its coinbase (works only if the node
        # exposes ETH and has a coinbase configured).
        if rpcs and probe_coinbase:
            for r in rpcs:
                if r.name in self.addr_by_name:
                    continue
                ok, res = r.try_call("eth_coinbase")
                if ok and res and hx(res) != 0:
                    addr = norm_addr(res)
                    self.name_by_addr[addr] = r.name
                    self.addr_by_name[r.name] = addr

    @property
    def missing(self):
        return [n for n in self.node_names if n not in self.addr_by_name]

    @property
    def complete(self):
        return not self.missing

    def name(self, addr):
        a = norm_addr(addr)
        return self.name_by_addr.get(a, "?" + a[:8])


# ============================================================
# Blocks
# ============================================================

def parse_block(b):
    txs = b.get("transactions", [])
    return {
        "number": hx(b["number"]),
        "timestamp": hx(b["timestamp"]),
        "miner": norm_addr(b["miner"]),
        "tx_count": len(txs),
        "tx_hashes": [t if isinstance(t, str) else t["hash"] for t in txs],
        "gas_used": hx(b["gasUsed"]),
        "gas_limit": hx(b["gasLimit"]),
        "hash": b["hash"],
    }


def fetch_blocks(rpc, numbers, workers=8):
    """{number: parsed block or None}. Raw RPC, so QBFT's long extraData
    does not trip web3.py's header validation."""
    numbers = sorted({int(n) for n in numbers if int(n) >= 0})

    def one(n):
        try:
            b = rpc.call("eth_getBlockByNumber", [hex(n), False])
            return n, (parse_block(b) if b else None)
        except Exception:  # noqa: BLE001
            return n, None

    if not numbers:
        return {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return dict(ex.map(one, numbers))


# ============================================================
# blocks
# ============================================================

def cmd_blocks(a):
    topo = load_json(a.topology)
    rpcs = build_rpcs(topo, a.host)
    rpc = pick_node(rpcs, a.node)
    d = NodeDirectory(topo, rpcs)

    head = hx(rpc.call("eth_blockNumber"))
    hi = a.end if a.end is not None else head
    lo = a.start if a.start is not None else max(0, hi - a.last + 1)
    blocks = fetch_blocks(rpc, range(lo, hi + 1))

    if d.missing:
        print(f"NOTE: no validator address for nodes {d.missing}; "
              f"proposers of those show as raw addresses.\n")

    print(f"{'block':>7} {'time(UTC)':>9} {'dt':>4} {'proposer':<12} "
          f"{'txs':>4} {'gas%':>6}")
    prev_ts = None
    seq = []
    tx_hist = Counter()
    empty = 0
    total_tx = 0
    for n in range(lo, hi + 1):
        b = blocks.get(n)
        if not b:
            print(f"{n:>7} (unavailable)")
            continue
        dt = "" if prev_ts is None else b["timestamp"] - prev_ts
        prev_ts = b["timestamp"]
        who = d.name(b["miner"])
        seq.append(who)
        gp = 100.0 * b["gas_used"] / b["gas_limit"] if b["gas_limit"] else 0
        print(f"{n:>7} {fmt_ts(b['timestamp']):>9} {dt!s:>4} {who:<12} "
              f"{b['tx_count']:>4} {gp:>5.2f}%")
        total_tx += b["tx_count"]
        if b["tx_count"] == 0:
            empty += 1
        else:
            tx_hist[b["tx_count"]] += 1

    print()
    print(f"blocks={len(seq)} empty={empty} non-empty={len(seq) - empty} "
          f"total_tx={total_tx}")
    print(f"tx-per-non-empty-block histogram: {dict(sorted(tx_hist.items()))}")

    counts = Counter(seq)
    print(f"distinct proposers: {len(counts)}  -> "
          + ", ".join(f"{k}:{v}" for k, v in sorted(counts.items())))

    # Round-robin check: gap (in blocks) between a proposer's consecutive turns.
    pos = defaultdict(list)
    for i, who in enumerate(seq):
        pos[who].append(i)
    gaps = set()
    for who, idxs in pos.items():
        for x, y in zip(idxs, idxs[1:]):
            gaps.add(y - x)
    if gaps:
        print(f"gaps between a proposer's consecutive turns: {sorted(gaps)} "
              f"({'round-robin over ' + str(len(counts)) + ' validators' if gaps == {len(counts)} else 'NOT a fixed round-robin'})")


# ============================================================
# pool
# ============================================================

def _snap_node(rpc, a, hashes):
    s = {"name": rpc.name}
    ok, v = rpc.try_call("eth_blockNumber")
    s["head"] = hx(v) if ok else None
    ok, v = rpc.try_call("net_peerCount")
    s["peers"] = hx(v) if ok else None
    ok, v = rpc.try_call("txpool_besuStatistics")
    s["stats"] = v if ok and isinstance(v, dict) else None
    s["stats_err"] = None if ok else v
    ok, v = rpc.try_call("txpool_besuPendingTransactions", [a.limit])
    s["pending"] = v if ok and isinstance(v, list) else None
    s["pending_err"] = None if ok else v
    if hashes:
        held = mined = 0
        for h in hashes:
            ok, tx = rpc.try_call("eth_getTransactionByHash", [h])
            if ok and tx:
                held += 1
                if tx.get("blockNumber"):
                    mined += 1
        s["held"], s["mined"] = held, mined
    return s


def _executability(rpc, pending):
    """Group a node's pending list by sender; flag nonce gaps."""
    by_sender = defaultdict(list)
    for t in pending:
        by_sender[t["from"].lower()].append(hx(t["nonce"]))
    rows = []
    for sender, nonces in by_sender.items():
        ok, latest = rpc.try_call("eth_getTransactionCount", [sender, "latest"])
        latest = hx(latest) if ok else None
        nonces.sort()
        contiguous = all(y == x + 1 for x, y in zip(nonces, nonces[1:]))
        executable = latest is not None and nonces[0] == latest and contiguous
        rows.append((sender, latest, nonces, executable))
    return rows


def cmd_pool(a):
    topo = load_json(a.topology)
    rpcs = build_rpcs(topo, a.host)

    hashes = []
    if a.detail:
        with open(a.detail) as f:
            hashes = [norm_hash(r["tx_hash"]) for r in csv.DictReader(f)
                      if r.get("tx_hash")][: a.max_hashes]

    try:
        while True:
            with ThreadPoolExecutor(max_workers=len(rpcs)) as ex:
                snaps = list(ex.map(lambda r: _snap_node(r, a, hashes), rpcs))

            print(f"\n=== txpool snapshot {datetime.now(timezone.utc):%H:%M:%S}Z ===")
            hdr = f"{'node':<12} {'head':>7} {'peers':>5} {'local':>6} {'remote':>6} {'listed':>6}"
            if hashes:
                hdr += f" {'held/' + str(len(hashes)):>9} {'mined':>6}"
            print(hdr)
            heads = [s["head"] for s in snaps if s["head"] is not None]
            for s in snaps:
                st = s["stats"] or {}
                line = (f"{s['name']:<12} {s['head']!s:>7} {s['peers']!s:>5} "
                        f"{st.get('localCount', '-')!s:>6} {st.get('remoteCount', '-')!s:>6} "
                        f"{(len(s['pending']) if s['pending'] is not None else '-')!s:>6}")
                if hashes:
                    line += f" {s['held']:>9} {s['mined']:>6}"
                print(line)

            if heads and max(heads) - min(heads) > 1:
                print(f"WARNING: nodes are at different heights "
                      f"({min(heads)}..{max(heads)}) - some node is lagging.")

            errs = {s["stats_err"] or s["pending_err"] for s in snaps
                    if s["stats_err"] or s["pending_err"]}
            if errs:
                print("txpool RPC errors (add TXPOOL to --rpc-http-api on the nodes): "
                      + "; ".join(sorted(errs)))

            # Nonce-gap / executability check on one node's pending list.
            ref = next((s for s in snaps if s["pending"]), None)
            if ref:
                rpc = next(r for r in rpcs if r.name == ref["name"])
                print(f"\nexecutability of {ref['name']}'s pending txs "
                      f"(non-executable = nonce gap, can never be selected):")
                for sender, latest, nonces, ok in _executability(rpc, ref["pending"]):
                    print(f"  {sender[:12]}...  chain_nonce={latest} "
                          f"pool_nonces={nonces}  "
                          f"{'executable' if ok else 'NOT EXECUTABLE (gap/stale)'}")

            if not a.watch:
                break
            time.sleep(a.watch)
    except KeyboardInterrupt:
        pass


# ============================================================
# leg
# ============================================================

def cmd_leg(a):
    topo = load_json(a.topology)
    rpcs = build_rpcs(topo, a.host)
    rpc = pick_node(rpcs, a.node)
    d = NodeDirectory(topo, rpcs)

    with open(a.detail) as f:
        rows = list(csv.DictReader(f))
    conf = [r for r in rows if r["status"] == "confirmed" and r.get("block_number")]
    print(f"{len(rows)} txs in CSV, {len(conf)} confirmed")
    if not conf:
        return

    nums = [int(r["block_number"]) for r in conf]
    head = hx(rpc.call("eth_blockNumber"))
    lo = max(0, min(nums) - a.margin)
    hi = min(head, max(nums) + a.margin)
    blocks = fetch_blocks(rpc, range(lo, hi + 1))
    ordered = sorted(n for n, b in blocks.items() if b)

    if d.missing:
        print(f"NOTE: no validator address for {d.missing}; "
              f"'mined by entry' cannot be evaluated for those nodes.")

    def first_after(ts):
        for n in ordered:
            if blocks[n]["timestamp"] >= int(ts):
                return n
        return None

    def next_turn(addr, start):
        if not addr or start is None:
            return None
        for n in ordered:
            if n >= start and blocks[n]["miner"] == addr:
                return n
        return None

    print()
    print(f"{'idx':>4} {'entry':<10} {'block':>7} {'proposer':<12} {'own?':>4} "
          f"{'lat(s)':>7} {'1st_blk':>8} {'entry_turn':>10} {'waited':>6}")

    own = known = at_entry_turn = at_first = 0
    senders = Counter()
    per_block = Counter()
    lats = []
    for r in sorted(conf, key=lambda r: int(r["tx_index"])):
        inc = int(r["block_number"])
        b = blocks.get(inc)
        miner_name = d.name(b["miner"]) if b else "?"
        entry = r["rpc_node"]
        senders[r["sender"]] += 1
        per_block[inc] += 1

        is_own = None
        if b and entry in d.addr_by_name:
            is_own = (b["miner"] == d.addr_by_name[entry])
            known += 1
            own += int(is_own)

        fa = first_after(float(r["submit_ts"])) if r.get("submit_ts") else None
        et = next_turn(d.addr_by_name.get(entry), fa)
        if fa is not None and inc <= fa + 1:
            at_first += 1
        if et is not None and abs(inc - et) <= 0:
            at_entry_turn += 1
        lat = float(r["latency_s"]) if r.get("latency_s") else None
        if lat is not None:
            lats.append(lat)
        waited = (inc - fa) if fa is not None else None
        print(f"{r['tx_index']:>4} {entry:<10} {inc:>7} {miner_name:<12} "
              f"{('Y' if is_own else 'N') if is_own is not None else '?':>4} "
              f"{(f'{lat:.1f}' if lat is not None else '-'):>7} "
              f"{fa!s:>8} {et!s:>10} {waited!s:>6}")

    n = len(conf)
    print()
    if known:
        print(f"mined by its own entry node : {own}/{known} ({100.0 * own / known:.0f}%)")
    print(f"mined within 1 block of first block after submit: {at_first}/{n}")
    print(f"mined exactly at entry node's next turn         : {at_entry_turn}/{n}")
    print(f"txs per block: {dict(sorted(Counter(per_block.values()).items()))} "
          f"(max {max(per_block.values())})")
    shared = {s: c for s, c in senders.items() if c > 1}
    print(f"distinct senders: {len(senders)}; senders with >1 tx (nonce chain): "
          f"{len(shared)}")
    if lats:
        lats.sort()
        print(f"latency min/p50/max: {lats[0]:.1f} / {lats[len(lats) // 2]:.1f} / {lats[-1]:.1f} s")

    print("\nverdict hints:")
    if known and own / known >= 0.8 and max(per_block.values()) <= 1:
        print("  * Each tx was mined alone, by its own entry node -> run "
              "'pool' and 'probe' to see whether the other nodes even HOLD the tx.")
    if shared:
        print("  * Some senders have several txs: same-sender nonce chains may "
              "serialize inclusion (check 'pool' executability output).")
    if not known:
        print("  * Proposer mapping missing: add validator 'address' to topology nodes.")


# ============================================================
# probe
# ============================================================

def cmd_probe(a):
    from eth_account import Account
    from eth_utils import to_checksum_address

    topo = load_json(a.topology)
    rpcs = build_rpcs(topo, a.host)
    d = NodeDirectory(topo, rpcs)
    accounts = load_json(a.accounts)

    targets = (list(range(len(rpcs))) if not a.nodes
               else [int(x) for x in a.nodes.split(",")])
    if len(accounts) < len(targets):
        raise SystemExit(f"need >= {len(targets)} accounts (one per probe), "
                         f"have {len(accounts)}")

    chain_id = hx(rpcs[0].call("eth_chainId"))
    gas_price = hx(rpcs[0].call("eth_gasPrice"))

    probes = []
    for i, k in enumerate(targets):
        acc = accounts[i]
        addr = to_checksum_address(acc["address"])
        nonce = hx(rpcs[k].call("eth_getTransactionCount", [addr, "pending"]))
        tx = {"chainId": chain_id, "nonce": nonce, "to": addr, "value": 0,
              "gas": 21000, "gasPrice": gas_price}
        signed = Account.sign_transaction(tx, acc["private_key"])
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        probes.append({"k": k, "entry": rpcs[k].name,
                       "raw": "0x" + bytes(raw).hex(), "hash": None,
                       "send_ts": None})

    wait_before = hx(rpcs[0].call("eth_blockNumber"))
    print(f"sending {len(probes)} probe txs (one per node, distinct senders); "
          f"head={wait_before}; broadcast={'ON' if a.broadcast else 'off'}")

    def send(p):
        p["send_ts"] = time.time()
        p["hash"] = rpcs[p["k"]].call("eth_sendRawTransaction", [p["raw"]])
        if a.broadcast:
            for j, r in enumerate(rpcs):
                if j != p["k"]:
                    r.try_call("eth_sendRawTransaction", [p["raw"]])

    with ThreadPoolExecutor(max_workers=len(probes)) as ex:
        list(ex.map(send, probes))

    time.sleep(a.settle)

    def holders(p):
        n = 0
        for r in rpcs:
            ok, tx = r.try_call("eth_getTransactionByHash", [p["hash"]])
            if ok and tx:
                n += 1
        return n

    with ThreadPoolExecutor(max_workers=len(probes)) as ex:
        for p, h in zip(probes, ex.map(holders, probes)):
            p["holders"] = h

    # wait for receipts (polled on the entry node)
    deadline = time.monotonic() + a.timeout
    pending = list(probes)
    while pending and time.monotonic() < deadline:
        still = []
        for p in pending:
            ok, rc = rpcs[p["k"]].try_call("eth_getTransactionReceipt", [p["hash"]])
            if ok and rc:
                p["block"] = hx(rc["blockNumber"])
                p["lat"] = time.time() - p["send_ts"]
            else:
                still.append(p)
        pending = still
        if pending:
            time.sleep(0.25)

    blocks = fetch_blocks(rpcs[0], [p["block"] for p in probes if p.get("block")])

    print()
    print(f"{'entry':<10} {'hash':<14} {'held_by':>9} {'block':>7} "
          f"{'proposer':<12} {'own?':>4} {'lat(s)':>7}")
    own = done = 0
    held = []
    for p in probes:
        b = blocks.get(p.get("block"))
        prop = d.name(b["miner"]) if b else "-"
        is_own = None
        if b and p["entry"] in d.addr_by_name:
            is_own = b["miner"] == d.addr_by_name[p["entry"]]
        if p.get("block"):
            done += 1
            own += int(bool(is_own))
        held.append(p["holders"])
        print(f"{p['entry']:<10} {p['hash'][:12]}.. "
              f"{str(p['holders']) + '/' + str(len(rpcs)):>9} "
              f"{p.get('block', '-')!s:>7} {prop:<12} "
              f"{('Y' if is_own else 'N') if is_own is not None else '?':>4} "
              f"{(f'{p['lat']:.1f}' if p.get('lat') is not None else 'timeout'):>7}")

    print()
    print(f"avg nodes holding the tx after {a.settle:.1f}s: "
          f"{sum(held) / len(held):.1f}/{len(rpcs)}")
    print(f"mined by entry node: {own}/{done}")
    blks = Counter(p["block"] for p in probes if p.get("block"))
    print(f"distinct blocks used: {len(blks)}  (txs per block: "
          f"{dict(sorted(Counter(blks.values()).items()))})")

    avg_hold = sum(held) / len(held)
    print("\nverdict:")
    if done and own / done >= 0.8 and avg_hold >= 0.8 * len(rpcs):
        print("  Txs are on (almost) every node but only the ENTRY node's block "
              "includes them -> proposer-side selection/pool problem, not "
              "propagation. Check Besu tx-pool flags and DEBUG logs on a proposer.")
    elif done and own / done >= 0.8 and avg_hold < 0.5 * len(rpcs):
        print("  Txs stay on their entry node -> propagation problem. Re-run "
              "with --broadcast; if that fixes it, the fanout code is what "
              "matters, otherwise peers/p2p are the issue.")
    elif done and len(blks) <= max(1, len(probes) // 4):
        print("  Txs were batched into few blocks -> the network behaves; the "
              "slowness comes from the harness (timing/nonces/phase).")
    else:
        print("  Mixed result; compare a run with and without --broadcast.")


# ============================================================
# validators
# ============================================================

def cmd_validators(a):
    topo = load_json(a.topology)
    rpcs = build_rpcs(topo, a.host)
    rpc = pick_node(rpcs, a.node)
    d = NodeDirectory(topo, rpcs)
    ok, vals = rpc.try_call("qbft_getValidatorsByBlockNumber", ["latest"])
    if not ok:
        print(f"qbft_getValidatorsByBlockNumber failed: {vals}\n"
              f"(enable the QBFT API in --rpc-http-api)")
        return
    print(f"{len(vals)} validators at latest block:")
    for v in vals:
        print(f"  {v}  ->  {d.name(v)}")
    unmapped = [n for n in d.node_names if n not in d.addr_by_name]
    if unmapped:
        print(f"\ntopology nodes without a validator address: {unmapped}")
    ok, m = rpc.try_call("qbft_getSignerMetrics")
    if ok and isinstance(m, list):
        print("\nproposed-block counts (qbft_getSignerMetrics):")
        for e in m:
            print(f"  {d.name(e['address'])}: proposed={e.get('proposedBlockCount')} "
                  f"last={e.get('lastProposedBlockNumber')}")


# ============================================================
# CLI
# ============================================================

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--topology", default="networkFiles/topology.json")
    p.add_argument("--host", default="localhost")
    p.add_argument("--node", default=None, help="node index or name used for reads (default: first)")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("blocks", help="recent blocks and proposer rotation")
    b.add_argument("--last", type=int, default=40)
    b.add_argument("--start", type=int)
    b.add_argument("--end", type=int)
    b.set_defaults(fn=cmd_blocks)

    q = sub.add_parser("pool", help="per-node txpool snapshot")
    q.add_argument("--detail", help="detail CSV from zkroam_workload.py (tx_hash column)")
    q.add_argument("--max-hashes", type=int, default=64)
    q.add_argument("--limit", type=int, default=500)
    q.add_argument("--watch", type=float, default=0, help="repeat every N seconds")
    q.set_defaults(fn=cmd_pool)

    l = sub.add_parser("leg", help="post-mortem of a detail CSV")
    l.add_argument("--detail", required=True)
    l.add_argument("--margin", type=int, default=20,
                   help="extra blocks fetched either side of the leg")
    l.set_defaults(fn=cmd_leg)

    pr = sub.add_parser("probe", help="send one tx per node and see who mines it")
    pr.add_argument("--accounts", default="accounts/accounts.json")
    pr.add_argument("--nodes", help="comma-separated node indices (default: all)")
    pr.add_argument("--broadcast", action="store_true",
                    help="also send each tx to every other node")
    pr.add_argument("--settle", type=float, default=1.0,
                    help="seconds to wait before counting nodes holding each tx")
    pr.add_argument("--timeout", type=float, default=90.0)
    pr.set_defaults(fn=cmd_probe)

    v = sub.add_parser("validators", help="QBFT validator set")
    v.set_defaults(fn=cmd_validators)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()