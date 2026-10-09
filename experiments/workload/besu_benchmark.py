#!/usr/bin/env python3

import argparse
import csv
import json
import os
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from web3 import Web3

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False


# ============================================================
# Configuration
# ============================================================

DEFAULT_GAS = 21_000
DEFAULT_VALUE = 1


# ============================================================
# Helpers
# ============================================================

def load_json(path):
    with open(path, "r") as f:
        return json.load(f)


def now():
    return time.monotonic()


def percentile(values, p):
    if not values:
        return 0.0

    values = sorted(values)

    if len(values) == 1:
        return values[0]

    k = (len(values) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(values))

    if f == c:
        return values[f]

    return values[f] + (values[c] - values[f]) * (k - f)


class ResourceMonitor:
    """
    Samples host-level CPU / memory / disk-IO utilization on a
    background thread for the duration of the benchmark, so
    throughput and latency numbers can be correlated against
    resource usage (e.g. to tell a gas-limit bottleneck apart
    from a CPU-, disk-, or GC-bound one).

    Runs entirely locally (via psutil) against the machine this
    script executes on. If the Besu nodes run on a different
    host, point this at that host instead (out of scope here,
    but the sample schema is the same either way).
    """

    def __init__(self, interval):
        self.interval = interval
        self.samples = []
        self._stop_event = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._phase = "idle"

    def set_phase(self, phase):
        """Tag subsequent samples with the current benchmark phase
        (e.g. 'submission', 'confirmation') so usage can be broken
        down per phase in the summary."""
        with self._lock:
            self._phase = phase

    def _sample_once(self):

        ts = time.time()

        with self._lock:
            phase = self._phase

        sample = {
            "ts": ts,
            "phase": phase,
            "cpu_percent": None,
            "cpu_percent_per_core": None,
            "mem_percent": None,
            "mem_used_mb": None,
            "mem_available_mb": None,
            "disk_read_mb": None,
            "disk_write_mb": None,
            "load_avg_1m": None,
        }

        if not PSUTIL_AVAILABLE:
            return sample

        try:
            sample["cpu_percent"] = psutil.cpu_percent(interval=None)
            sample["cpu_percent_per_core"] = ",".join(
                f"{c:.1f}" for c in psutil.cpu_percent(
                    interval=None, percpu=True
                )
            )
        except Exception:
            pass

        try:
            vm = psutil.virtual_memory()
            sample["mem_percent"] = vm.percent
            sample["mem_used_mb"] = vm.used / (1024 * 1024)
            sample["mem_available_mb"] = vm.available / (1024 * 1024)
        except Exception:
            pass

        try:
            io = psutil.disk_io_counters()
            if io is not None:
                sample["disk_read_mb"] = io.read_bytes / (1024 * 1024)
                sample["disk_write_mb"] = io.write_bytes / (1024 * 1024)
        except Exception:
            pass

        try:
            sample["load_avg_1m"] = os.getloadavg()[0]
        except Exception:
            pass

        return sample

    def _run(self):

        # Prime psutil's internal CPU-percent baseline; the first
        # call after this returns 0.0 / meaningless deltas otherwise.
        if PSUTIL_AVAILABLE:
            try:
                psutil.cpu_percent(interval=None)
                psutil.cpu_percent(interval=None, percpu=True)
            except Exception:
                pass

        while not self._stop_event.is_set():

            sample = self._sample_once()

            with self._lock:
                self.samples.append(sample)

            self._stop_event.wait(self.interval)

    def start(self):

        if not PSUTIL_AVAILABLE:
            print(
                "WARNING: psutil not installed - resource "
                "monitoring will be skipped. "
                "Install with: pip install psutil"
            )

        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
        )
        self._thread.start()

    def stop(self):

        self._stop_event.set()

        if self._thread is not None:
            self._thread.join(timeout=self.interval + 5)

    def summary(self):
        """Aggregate overall and per-phase CPU/memory stats."""

        with self._lock:
            samples = list(self.samples)

        if not samples:
            return {
                "sample_count": 0,
                "cpu_avg_pct": None,
                "cpu_max_pct": None,
                "mem_avg_pct": None,
                "mem_max_pct": None,
                "load_avg_1m_max": None,
            }

        cpu_vals = [
            s["cpu_percent"] for s in samples
            if s["cpu_percent"] is not None
        ]

        mem_vals = [
            s["mem_percent"] for s in samples
            if s["mem_percent"] is not None
        ]

        load_vals = [
            s["load_avg_1m"] for s in samples
            if s["load_avg_1m"] is not None
        ]

        return {
            "sample_count": len(samples),
            "cpu_avg_pct": (
                sum(cpu_vals) / len(cpu_vals) if cpu_vals else None
            ),
            "cpu_max_pct": max(cpu_vals) if cpu_vals else None,
            "mem_avg_pct": (
                sum(mem_vals) / len(mem_vals) if mem_vals else None
            ),
            "mem_max_pct": max(mem_vals) if mem_vals else None,
            "load_avg_1m_max": max(load_vals) if load_vals else None,
        }

    def phase_summary(self):
        """Same aggregates as summary(), broken down per phase."""

        with self._lock:
            samples = list(self.samples)

        by_phase = defaultdict(list)

        for s in samples:
            by_phase[s["phase"]].append(s)

        result = {}

        for phase, phase_samples in by_phase.items():

            cpu_vals = [
                s["cpu_percent"] for s in phase_samples
                if s["cpu_percent"] is not None
            ]

            mem_vals = [
                s["mem_percent"] for s in phase_samples
                if s["mem_percent"] is not None
            ]

            result[phase] = {
                "sample_count": len(phase_samples),
                "cpu_avg_pct": (
                    sum(cpu_vals) / len(cpu_vals) if cpu_vals else None
                ),
                "cpu_max_pct": max(cpu_vals) if cpu_vals else None,
                "mem_avg_pct": (
                    sum(mem_vals) / len(mem_vals) if mem_vals else None
                ),
                "mem_max_pct": max(mem_vals) if mem_vals else None,
            }

        return result

    def write_csv(self, path):

        os.makedirs(
            os.path.dirname(path)
            if os.path.dirname(path)
            else ".",
            exist_ok=True,
        )

        with self._lock:
            samples = list(self.samples)

        fields = [
            "ts",
            "phase",
            "cpu_percent",
            "cpu_percent_per_core",
            "mem_percent",
            "mem_used_mb",
            "mem_available_mb",
            "disk_read_mb",
            "disk_write_mb",
            "load_avg_1m",
        ]

        with open(path, "w", newline="") as f:

            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()

            for s in samples:
                writer.writerow(s)

            if not PSUTIL_AVAILABLE:
                # Every column above is a real one - not blank because the
                # run was actually idle - but None because psutil couldn't
                # be imported at all. Loud in the CSV itself, not just a
                # startup print easy to miss in a long run's terminal log.
                writer.writerow({})
                writer.writerow({
                    "ts": "NOTE",
                    "phase": "psutil not installed - all metric columns "
                             "above are empty, not actually zero/idle. "
                             "Install with: pip install psutil",
                })

        print(f"Wrote: {path}")


def build_rpc_endpoints(topology):
    endpoints = []

    for node in topology["nodes"]:
        name = node["name"]
        port = node["host_rpc_port"]

        endpoints.append(
            {
                "name": name,
                "url": f"http://localhost:{port}",
                "port": port,
            }
        )

    return endpoints


# ============================================================
# RPC setup
# ============================================================

def connect_web3(endpoints):

    web3s = []

    print("Discovered RPC endpoints:")
    print("------------------------------------------")

    for ep in endpoints:
        print(f"  {ep['name']:<10} -> {ep['url']}")

    print("------------------------------------------")

    chain_id = None

    for ep in endpoints:

        print(f"Connecting to {ep['name']} ({ep['url']})...")

        w3 = Web3(Web3.HTTPProvider(
            ep["url"],
            request_kwargs={"timeout": 30},
        ))

        if not w3.is_connected():
            raise RuntimeError(
                f"Could not connect to {ep['url']}"
            )

        cid = w3.eth.chain_id

        print(f"  Connected. chain_id={cid}")

        if chain_id is None:
            chain_id = cid
        elif cid != chain_id:
            raise RuntimeError(
                f"Chain ID mismatch: {ep['name']} has {cid}, "
                f"expected {chain_id}"
            )

        web3s.append(w3)

    return web3s, chain_id


# ============================================================
# Account loading
# ============================================================

def load_accounts(path):

    data = load_json(path)

    accounts = []

    for item in data:

        address = Web3.to_checksum_address(item["address"])
        private_key = item["private_key"]

        accounts.append(
            {
                "index": item["index"],
                "address": address,
                "private_key": private_key,
            }
        )

    return accounts


# ============================================================
# Account -> RPC assignment
# ============================================================

def assign_accounts(accounts, endpoints):

    assignment = {}

    for i, account in enumerate(accounts):

        rpc_index = i % len(endpoints)

        assignment[account["index"]] = rpc_index

    return assignment


# ============================================================
# Nonce preparation
# ============================================================

def load_nonces(accounts, assignment, web3s):

    print()
    print(f"Reading pending nonce for {len(accounts)} accounts (parallel)...")
    print()

    def fetch(account):
        rpc_index = assignment[account["index"]]
        w3 = web3s[rpc_index]
        nonce = w3.eth.get_transaction_count(account["address"], "pending")
        return account["index"], nonce

    nonces = {}
    with ThreadPoolExecutor(max_workers=min(64, len(accounts) or 1)) as executor:
        for account_index, nonce in executor.map(fetch, accounts):
            nonces[account_index] = nonce

    return nonces


# ============================================================
# Transaction preparation
# ============================================================

def prepare_transactions(
    accounts,
    assignment,
    nonces,
    web3s,
    num_tx,
    chain_id,
    gas,
    value,
):

    if not accounts:
        raise RuntimeError("No accounts available.")

    transactions = []

    # Keep nonce allocation local to each account.
    next_nonce = dict(nonces)

    for tx_index in range(num_tx):

        account = accounts[tx_index % len(accounts)]

        account_index = account["index"]
        rpc_index = assignment[account_index]

        w3 = web3s[rpc_index]

        nonce = next_nonce[account_index]
        next_nonce[account_index] += 1

        tx = {
            "chainId": chain_id,
            "nonce": nonce,
            "to": account["address"],
            "value": value,
            "gas": gas,
            "gasPrice": w3.eth.gas_price,
        }

        signed = w3.eth.account.sign_transaction(
            tx,
            account["private_key"],
        )

        raw = signed.raw_transaction

        transactions.append(
            {
                "tx_index": tx_index,
                "account_index": account_index,
                "sender": account["address"],
                "rpc_index": rpc_index,
                "raw": raw,
                "nonce": nonce,
                "tx_hash": None,
                "submit_ts": None,
                "confirm_ts": None,
                "block_number": None,
                "status": "prepared",
                "error": None,
            }
        )

    return transactions


# ============================================================
# Submission
# ============================================================

def submit_transaction(item, web3s):

    rpc_index = item["rpc_index"]
    w3 = web3s[rpc_index]

    submit_ts = time.time()

    try:

        tx_hash = w3.eth.send_raw_transaction(
            item["raw"]
        )

        item["tx_hash"] = tx_hash.hex()
        item["submit_ts"] = submit_ts
        item["status"] = "submitted"

        return item

    except Exception as e:

        item["submit_ts"] = submit_ts
        item["status"] = "failed"
        item["error"] = str(e)

        return item


# ============================================================
# Rate-controlled producer
# ============================================================

def submit_all(
    transactions,
    web3s,
    target_rate,
    workers,
):

    print()
    print("Starting transaction producer...")
    print(
        f"Target submission rate : {target_rate:.2f} tx/s"
    )
    print(
        f"Submission workers      : {workers}"
    )
    print()

    start = time.monotonic()

    submitted = 0
    failed = 0

    lock = threading.Lock()

    # We use a bounded submission window.
    #
    # The producer does NOT wait for receipts.
    #
    # Rate limiting is done by scheduling transactions
    # at target_rate intervals.

    with ThreadPoolExecutor(
        max_workers=workers
    ) as executor:

        futures = []

        next_send_time = start

        for item in transactions:

            # Rate limiter.
            if target_rate > 0:

                next_send_time = max(
                    next_send_time,
                    start + (item["tx_index"] / target_rate),
                )

                sleep_time = (
                    next_send_time - time.monotonic()
                )

                if sleep_time > 0:
                    time.sleep(sleep_time)

            future = executor.submit(
                submit_transaction,
                item,
                web3s,
            )

            futures.append(future)

            next_send_time = time.monotonic()

            # Avoid creating an enormous future queue.
            if len(futures) >= workers * 4:

                completed = futures[:workers]
                futures = futures[workers:]

                for f in as_completed(completed):

                    result = f.result()

                    with lock:

                        if result["status"] == "submitted":
                            submitted += 1
                        else:
                            failed += 1

        # Remaining submissions.
        for f in as_completed(futures):

            result = f.result()

            with lock:

                if result["status"] == "submitted":
                    submitted += 1
                else:
                    failed += 1

    end = time.monotonic()

    duration = end - start

    rate = (
        submitted / duration
        if duration > 0
        else 0
    )

    print()
    print("Producer finished.")
    print("------------------------------------------")
    print(f"Submitted : {submitted}/{len(transactions)}")
    print(f"Failed    : {failed}/{len(transactions)}")
    print(f"Duration  : {duration:.3f}s")
    print(f"Rate      : {rate:.2f} tx/s")
    print("------------------------------------------")

    return submitted


# ============================================================
# Receipt polling
# ============================================================

def poll_receipt(item, web3s, timeout, poll_interval):

    rpc_index = item["rpc_index"]

    # Use the same RPC to which the transaction was submitted.
    w3 = web3s[rpc_index]

    tx_hash = item["tx_hash"]

    start = time.monotonic()

    while True:

        elapsed = time.monotonic() - start

        if elapsed >= timeout:

            item["status"] = "timeout"
            item["error"] = (
                f"Receipt timeout after {timeout:.1f}s"
            )

            return item

        try:

            receipt = w3.eth.get_transaction_receipt(
                tx_hash
            )

            if receipt is not None:

                item["confirm_ts"] = time.time()

                item["block_number"] = (
                    receipt["blockNumber"]
                )

                status = receipt.get("status")

                if status == 1:
                    item["status"] = "confirmed"
                else:
                    item["status"] = "reverted"

                return item

        except Exception:
            # Transaction may simply not be visible yet.
            pass

        time.sleep(poll_interval)


# ============================================================
# Parallel receipt polling
# ============================================================

def poll_all_receipts(
    transactions,
    web3s,
    workers,
    timeout,
    poll_interval,
):

    submitted = [
        x for x in transactions
        if x["status"] == "submitted"
        and x["tx_hash"] is not None
    ]

    print()
    print("Starting receipt polling...")
    print("------------------------------------------")
    print(
        f"Transactions to confirm : {len(submitted)}"
    )
    print(
        f"Receipt workers         : {workers}"
    )
    print(
        f"Receipt timeout         : {timeout:.1f}s"
    )
    print("------------------------------------------")
    print()

    start = time.monotonic()

    confirmed = 0
    reverted = 0
    timeout_count = 0

    with ThreadPoolExecutor(
        max_workers=workers
    ) as executor:

        futures = {
            executor.submit(
                poll_receipt,
                item,
                web3s,
                timeout,
                poll_interval,
            ): item
            for item in submitted
        }

        total = len(futures)

        for i, future in enumerate(
            as_completed(futures),
            start=1,
        ):

            result = future.result()

            if result["status"] == "confirmed":
                confirmed += 1

            elif result["status"] == "reverted":
                reverted += 1

            elif result["status"] == "timeout":
                timeout_count += 1

            if i % 25 == 0 or i == total:

                print(
                    f"Confirmed: {confirmed}/{total} "
                    f"| Reverted: {reverted} "
                    f"| Timeout: {timeout_count}"
                )

    duration = time.monotonic() - start

    print()
    print("Receipt polling finished.")
    print("------------------------------------------")
    print(f"Confirmed : {confirmed}")
    print(f"Reverted  : {reverted}")
    print(f"Timeout   : {timeout_count}")
    print(f"Duration  : {duration:.3f}s")
    print("------------------------------------------")

    return confirmed


# ============================================================
# Statistics
# ============================================================

def compute_statistics(transactions):
    """
    Compute aggregate benchmark statistics as a plain dict.
    Shared by print_statistics() and write_summary() so the
    console output and the saved summary can never drift apart.
    """

    confirmed = [
        x for x in transactions
        if x["status"] == "confirmed"
        and x["submit_ts"] is not None
        and x["confirm_ts"] is not None
    ]

    submitted = [
        x for x in transactions
        if x["status"] in (
            "submitted",
            "confirmed",
            "reverted",
            "timeout",
        )
    ]

    reverted = [
        x for x in transactions
        if x["status"] == "reverted"
    ]

    timed_out = [
        x for x in transactions
        if x["status"] == "timeout"
    ]

    failed = [
        x for x in transactions
        if x["status"] == "failed"
    ]

    stats = {
        "prepared": len(transactions),
        "submitted": len(submitted),
        "confirmed": len(confirmed),
        "reverted": len(reverted),
        "timeout": len(timed_out),
        "failed_submission": len(failed),
        "success_rate_pct": None,
        "submit_duration_s": None,
        "submission_throughput_tx_s": None,
        "confirm_duration_s": None,
        "confirmation_throughput_tx_s": None,
        "latency_avg_s": None,
        "latency_p50_s": None,
        "latency_p95_s": None,
        "latency_p99_s": None,
        "latency_max_s": None,
    }

    if submitted:
        stats["success_rate_pct"] = (
            len(confirmed) / len(submitted)
        ) * 100

    if confirmed:

        latencies = [
            x["confirm_ts"] - x["submit_ts"]
            for x in confirmed
        ]

        first_submit = min(x["submit_ts"] for x in confirmed)
        last_submit = max(x["submit_ts"] for x in confirmed)
        first_confirm = min(x["confirm_ts"] for x in confirmed)
        last_confirm = max(x["confirm_ts"] for x in confirmed)

        submit_duration = last_submit - first_submit
        confirmation_duration = last_confirm - first_confirm

        stats["submit_duration_s"] = submit_duration
        stats["confirm_duration_s"] = confirmation_duration

        if submit_duration > 0:
            stats["submission_throughput_tx_s"] = (
                len(submitted) / submit_duration
            )

        if confirmation_duration > 0:
            stats["confirmation_throughput_tx_s"] = (
                len(confirmed) / confirmation_duration
            )

        stats["latency_avg_s"] = sum(latencies) / len(latencies)
        stats["latency_p50_s"] = percentile(latencies, 50)
        stats["latency_p95_s"] = percentile(latencies, 95)
        stats["latency_p99_s"] = percentile(latencies, 99)
        stats["latency_max_s"] = max(latencies)

    return stats


def compute_rpc_statistics(transactions, endpoints):
    """
    Compute per-RPC-node stats as a plain dict, keyed by node name.
    Shared by print_rpc_statistics() and write_summary().
    """

    stats = defaultdict(
        lambda: {
            "submitted": 0,
            "confirmed": 0,
            "failed": 0,
            "timeout": 0,
        }
    )

    for item in transactions:

        node = endpoints[item["rpc_index"]]["name"]
        status = item["status"]

        if status in (
            "submitted",
            "confirmed",
            "reverted",
            "timeout",
        ):
            stats[node]["submitted"] += 1

        if status == "confirmed":
            stats[node]["confirmed"] += 1

        elif status == "failed":
            stats[node]["failed"] += 1

        elif status == "timeout":
            stats[node]["timeout"] += 1

    # Ensure every endpoint appears, even with zero activity.
    ordered = {}

    for ep in endpoints:
        ordered[ep["name"]] = stats[ep["name"]]

    return ordered


def print_statistics(transactions):

    stats = compute_statistics(transactions)

    print()
    print("=" * 42)
    print("FINAL BENCHMARK RESULTS")
    print("=" * 42)

    print(f"Prepared transactions : {stats['prepared']}")
    print(f"Submitted             : {stats['submitted']}")
    print(f"Confirmed             : {stats['confirmed']}")
    print(f"Failed submission     : {stats['failed_submission']}")

    if stats["success_rate_pct"] is not None:
        print(
            f"Success rate          : "
            f"{stats['success_rate_pct']:.2f}%"
        )

    if stats["latency_avg_s"] is not None:

        print()
        print("Submission:")
        print(
            f"  Duration              : "
            f"{stats['submit_duration_s']:.3f}s"
        )

        if stats["submission_throughput_tx_s"] is not None:
            print(
                f"  Submission throughput : "
                f"{stats['submission_throughput_tx_s']:.2f} tx/s"
            )

        print()
        print("Confirmation:")
        print(
            f"  Duration              : "
            f"{stats['confirm_duration_s']:.3f}s"
        )

        if stats["confirmation_throughput_tx_s"] is not None:
            print(
                f"  Confirmation throughput: "
                f"{stats['confirmation_throughput_tx_s']:.2f} tx/s"
            )

        print()
        print("End-to-end latency:")
        print(f"  Average               : {stats['latency_avg_s']:.3f}s")
        print(f"  p50                   : {stats['latency_p50_s']:.3f}s")
        print(f"  p95                   : {stats['latency_p95_s']:.3f}s")
        print(f"  p99                   : {stats['latency_p99_s']:.3f}s")
        print(f"  Maximum               : {stats['latency_max_s']:.3f}s")

    print("=" * 42)


# ============================================================
# Per-RPC statistics
# ============================================================

def print_rpc_statistics(transactions, endpoints):

    stats = compute_rpc_statistics(transactions, endpoints)

    print()
    print("Per-RPC endpoint:")
    print("------------------------------------------")

    for ep in endpoints:

        node = ep["name"]
        s = stats[node]

        print(
            f"  {node:<10} "
            f"submitted={s['submitted']:4d} "
            f"confirmed={s['confirmed']:4d} "
            f"failed={s['failed']:4d} "
            f"timeout={s['timeout']:4d}"
        )

    print("------------------------------------------")


# ============================================================
# CSV output (per-transaction detail)
# ============================================================

def write_csv(path, transactions, endpoints):

    os.makedirs(
        os.path.dirname(path)
        if os.path.dirname(path)
        else ".",
        exist_ok=True,
    )

    fields = [
        "tx_index",
        "account_index",
        "sender",
        "rpc_node",
        "rpc_endpoint",
        "tx_hash",
        "nonce",
        "submit_ts",
        "confirm_ts",
        "latency_s",
        "block_number",
        "status",
        "error",
    ]

    with open(
        path,
        "w",
        newline="",
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=fields,
        )

        writer.writeheader()

        for item in transactions:

            latency = None

            if (
                item["submit_ts"] is not None
                and item["confirm_ts"] is not None
            ):
                latency = (
                    item["confirm_ts"]
                    - item["submit_ts"]
                )

            writer.writerow(
                {
                    "tx_index": item["tx_index"],
                    "account_index": item["account_index"],
                    "sender": item["sender"],
                    "rpc_node": endpoints[
                        item["rpc_index"]
                    ]["name"],
                    "rpc_endpoint": endpoints[
                        item["rpc_index"]
                    ]["url"],
                    "tx_hash": item["tx_hash"],
                    "nonce": item["nonce"],
                    "submit_ts": item["submit_ts"],
                    "confirm_ts": item["confirm_ts"],
                    "latency_s": latency,
                    "block_number": item["block_number"],
                    "status": item["status"],
                    "error": item["error"],
                }
            )

    print(f"Wrote: {path}")


# ============================================================
# CSV output (results summary)
# ============================================================

def write_summary(
    path,
    transactions,
    endpoints,
    chain_id,
    args,
    resource_monitor=None,
):
    """
    Write a single results-summary CSV: one row of overall
    benchmark stats followed by one row per RPC node. This is
    separate from write_csv(), which stores per-transaction detail.

    If a ResourceMonitor is supplied, its overall and per-phase
    CPU/memory aggregates are appended as additional sections,
    so a throughput ceiling can be correlated against host
    resource usage (CPU, memory, load average) directly in the
    same file.
    """

    os.makedirs(
        os.path.dirname(path)
        if os.path.dirname(path)
        else ".",
        exist_ok=True,
    )

    stats = compute_statistics(transactions)
    rpc_stats = compute_rpc_statistics(transactions, endpoints)

    with open(path, "w", newline="") as f:

        writer = csv.writer(f)

        # ---- Run configuration ----
        writer.writerow(["section", "run_config"])
        writer.writerow(["chain_id", chain_id])
        writer.writerow(["num_tx_requested", args.num_tx])
        writer.writerow(["target_rate_tx_s", args.rate])
        writer.writerow(["submit_workers", args.workers])
        writer.writerow(["receipt_workers", args.receipt_workers])
        writer.writerow(["receipt_timeout_s", args.receipt_timeout])
        writer.writerow(["poll_interval_s", args.poll_interval])
        writer.writerow(["gas_per_tx", args.gas])
        writer.writerow(["value_per_tx", args.value])
        writer.writerow(["rpc_node_count", len(endpoints)])
        writer.writerow([])

        # ---- Overall results ----
        writer.writerow(["section", "overall_results"])
        writer.writerow(["metric", "value"])

        for key, value in stats.items():

            if isinstance(value, float):
                writer.writerow([key, f"{value:.6f}"])
            else:
                writer.writerow([key, value])

        writer.writerow([])

        # ---- Per-RPC breakdown ----
        writer.writerow(["section", "per_rpc_results"])
        writer.writerow(
            ["rpc_node", "rpc_endpoint", "submitted", "confirmed", "failed", "timeout"]
        )

        for ep in endpoints:

            node = ep["name"]
            s = rpc_stats[node]

            writer.writerow(
                [
                    node,
                    ep["url"],
                    s["submitted"],
                    s["confirmed"],
                    s["failed"],
                    s["timeout"],
                ]
            )

        # ---- Host resource usage (correlate bottlenecks) ----
        if resource_monitor is not None:

            writer.writerow([])
            writer.writerow(["section", "resource_usage_overall"])
            writer.writerow(["metric", "value"])

            overall = resource_monitor.summary()

            for key, value in overall.items():

                if isinstance(value, float):
                    writer.writerow([key, f"{value:.2f}"])
                else:
                    writer.writerow([key, value])

            writer.writerow([])
            writer.writerow(["section", "resource_usage_by_phase"])
            writer.writerow(
                [
                    "phase",
                    "sample_count",
                    "cpu_avg_pct",
                    "cpu_max_pct",
                    "mem_avg_pct",
                    "mem_max_pct",
                ]
            )

            for phase, s in resource_monitor.phase_summary().items():

                writer.writerow(
                    [
                        phase,
                        s["sample_count"],
                        (
                            f"{s['cpu_avg_pct']:.2f}"
                            if s["cpu_avg_pct"] is not None
                            else ""
                        ),
                        (
                            f"{s['cpu_max_pct']:.2f}"
                            if s["cpu_max_pct"] is not None
                            else ""
                        ),
                        (
                            f"{s['mem_avg_pct']:.2f}"
                            if s["mem_avg_pct"] is not None
                            else ""
                        ),
                        (
                            f"{s['mem_max_pct']:.2f}"
                            if s["mem_max_pct"] is not None
                            else ""
                        ),
                    ]
                )

            if not PSUTIL_AVAILABLE:
                writer.writerow([])
                writer.writerow(
                    [
                        "note",
                        "psutil not installed - resource "
                        "usage columns are empty. "
                        "Install with: pip install psutil",
                    ]
                )

    print(f"Wrote: {path}")


# ============================================================
# Main
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description=(
            "Besu QBFT transaction benchmark with "
            "decoupled transaction submission and "
            "receipt polling."
        )
    )

    parser.add_argument(
        "--topology",
        default="networkFiles/topology.json",
    )

    parser.add_argument(
        "--accounts",
        default="networkFiles/accounts/accounts.json",
    )

    parser.add_argument(
        "--num-tx",
        type=int,
        default=500,
    )

    parser.add_argument(
        "--rate",
        type=float,
        default=100.0,
        help="Target transaction submission rate.",
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=32,
        help="Concurrent transaction submission workers.",
    )

    parser.add_argument(
        "--receipt-workers",
        type=int,
        default=16,
        help="Concurrent receipt polling workers.",
    )

    parser.add_argument(
        "--receipt-timeout",
        type=float,
        default=120.0,
        help="Maximum time to wait for each receipt.",
    )

    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.25,
        help="Receipt polling interval in seconds.",
    )

    parser.add_argument(
        "--gas",
        type=int,
        default=DEFAULT_GAS,
    )

    parser.add_argument(
        "--value",
        type=int,
        default=DEFAULT_VALUE,
    )

    parser.add_argument(
        "--out",
        default="results/benchmark.csv",
    )

    parser.add_argument(
        "--summary-out",
        default="results/summary.csv",
        help="Path to write the aggregate results-summary CSV.",
    )

    parser.add_argument(
        "--monitor-resources",
        action="store_true",
        default=True,
        help="Sample host CPU/memory/load during the run (default: on).",
    )

    parser.add_argument(
        "--no-monitor-resources",
        dest="monitor_resources",
        action="store_false",
        help="Disable host resource monitoring.",
    )

    parser.add_argument(
        "--monitor-interval",
        type=float,
        default=1.0,
        help="Resource sampling interval in seconds.",
    )

    parser.add_argument(
        "--resource-out",
        default="results/resource_usage.csv",
        help="Path to write the resource-usage time-series CSV.",
    )

    args = parser.parse_args()

    # --------------------------------------------------------
    # Start resource monitoring (covers the whole run, including
    # topology/account loading, so idle baseline usage is visible
    # alongside submission/confirmation phases).
    # --------------------------------------------------------

    resource_monitor = None

    if args.monitor_resources:
        resource_monitor = ResourceMonitor(args.monitor_interval)
        resource_monitor.start()

    # --------------------------------------------------------
    # Load topology
    # --------------------------------------------------------

    topology = load_json(args.topology)

    endpoints = build_rpc_endpoints(topology)

    # --------------------------------------------------------
    # Connect
    # --------------------------------------------------------

    web3s, chain_id = connect_web3(
        endpoints
    )

    # --------------------------------------------------------
    # Load accounts
    # --------------------------------------------------------

    accounts = load_accounts(
        args.accounts
    )

    if not accounts:
        raise RuntimeError(
            "No workload accounts found."
        )

    print()
    print(
        f"Loaded {len(accounts)} funded "
        f"workload accounts."
    )

    # --------------------------------------------------------
    # Assign accounts to RPCs
    # --------------------------------------------------------

    assignment = assign_accounts(
        accounts,
        endpoints,
    )

    distribution = defaultdict(int)

    for account in accounts:

        rpc_index = assignment[
            account["index"]
        ]

        distribution[rpc_index] += 1

    print()
    print("Account distribution:")
    print("------------------------------------------")

    for i, ep in enumerate(endpoints):

        print(
            f"  {ep['name']:<10}: "
            f"{distribution[i]} accounts"
        )

    print("------------------------------------------")

    # --------------------------------------------------------
    # Nonces
    # --------------------------------------------------------

    nonces = load_nonces(
        accounts,
        assignment,
        web3s,
    )

    # --------------------------------------------------------
    # Prepare and sign transactions
    # --------------------------------------------------------

    print()
    print(
        f"Preparing and signing "
        f"{args.num_tx} transactions..."
    )

    transactions = prepare_transactions(
        accounts=accounts,
        assignment=assignment,
        nonces=nonces,
        web3s=web3s,
        num_tx=args.num_tx,
        chain_id=chain_id,
        gas=args.gas,
        value=args.value,
    )

    # --------------------------------------------------------
    # Benchmark information
    # --------------------------------------------------------

    print()
    print("=" * 42)
    print("BESU QBFT TRANSACTION BENCHMARK")
    print("=" * 42)

    print(
        f"Transactions       : {args.num_tx}"
    )

    print(
        f"Target rate        : {args.rate:.2f} tx/s"
    )

    print(
        f"Accounts           : {len(accounts)}"
    )

    print(
        f"RPC nodes          : {len(endpoints)}"
    )

    print(
        f"Submit workers     : {args.workers}"
    )

    print(
        f"Receipt workers    : {args.receipt_workers}"
    )

    print(
        f"Chain ID           : {chain_id}"
    )

    print(
        f"Gas / transaction  : {args.gas}"
    )

    print("=" * 42)

    # --------------------------------------------------------
    # PHASE 1:
    # Transaction submission
    # --------------------------------------------------------

    if resource_monitor is not None:
        resource_monitor.set_phase("submission")

    submit_all(
        transactions=transactions,
        web3s=web3s,
        target_rate=args.rate,
        workers=args.workers,
    )

    # --------------------------------------------------------
    # PHASE 2:
    # Receipt polling
    # --------------------------------------------------------

    if resource_monitor is not None:
        resource_monitor.set_phase("confirmation")

    poll_all_receipts(
        transactions=transactions,
        web3s=web3s,
        workers=args.receipt_workers,
        timeout=args.receipt_timeout,
        poll_interval=args.poll_interval,
    )

    if resource_monitor is not None:
        resource_monitor.set_phase("idle")
        resource_monitor.stop()

    # --------------------------------------------------------
    # Results
    # --------------------------------------------------------

    print_statistics(
        transactions
    )

    print_rpc_statistics(
        transactions,
        endpoints,
    )

    if resource_monitor is not None:

        overall = resource_monitor.summary()

        print()
        print("Host resource usage:")
        print("------------------------------------------")

        if overall["cpu_avg_pct"] is not None:
            print(
                f"  CPU avg / max         : "
                f"{overall['cpu_avg_pct']:.1f}% / "
                f"{overall['cpu_max_pct']:.1f}%"
            )
            print(
                f"  Memory avg / max      : "
                f"{overall['mem_avg_pct']:.1f}% / "
                f"{overall['mem_max_pct']:.1f}%"
            )
            if overall["load_avg_1m_max"] is not None:
                print(
                    f"  Load avg (1m) max     : "
                    f"{overall['load_avg_1m_max']:.2f}"
                )
        else:
            print(
                "  (psutil not installed - run "
                "`pip install psutil` to enable)"
            )

        print("------------------------------------------")

    write_csv(
        args.out,
        transactions,
        endpoints,
    )

    # Aggregate results-summary CSV (overall + per-RPC stats +
    # resource-usage aggregates, if monitoring was enabled).
    write_summary(
        args.summary_out,
        transactions,
        endpoints,
        chain_id,
        args,
        resource_monitor=resource_monitor,
    )

    # Full resource-usage time series, for plotting CPU/mem
    # against the submission/confirmation timeline.
    if resource_monitor is not None:
        resource_monitor.write_csv(args.resource_out)


if __name__ == "__main__":
    main()