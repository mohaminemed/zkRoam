#!/usr/bin/env python3
"""
Builds docker-compose.yml from networkFiles/topology.json.

Shared by several experiments. Per-node network shaping is chosen by what the
topology provides:
  - "profile" (tier experiments)  -> TC_DELAY_MS / TC_JITTER_MS / TC_RATE_MBIT / TC_LOSS_PCT
  - "egress_rules" (WAN)          -> networkFiles/tc/<node>.rules mounted at /config/tc.rules,
                                     TC_RULES_FILE env var points to it
  - neither                       -> no shaping

Bootnode selection:
  - if nodes have a "tier": the first 3 "core" nodes
  - elif nodes have a "region": the first node of each region
  - else: the first 3 nodes
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOPOLOGY_FILE = os.path.join(ROOT, "networkFiles", "topology.json")
KEYS_DIR = os.path.join(ROOT, "networkFiles", "keys")
TC_DIR = os.path.join(ROOT, "networkFiles", "tc")
OUT_FILE = os.path.join(ROOT, "docker-compose.yml")


def enode_url(address: str, ip: str, port: int) -> str:
    pub_path = os.path.join(KEYS_DIR, address, "key.pub")
    with open(pub_path) as f:
        pub = f.read().strip()
    pub_hex = pub[2:] if pub.startswith("0x") else pub
    if pub_hex.startswith("04"):
        pub_hex = pub_hex[2:]
    return f"enode://{pub_hex}@{ip}:{port}"


def pick_bootnodes(nodes: list) -> tuple[list, str]:
    if all("tier" in n for n in nodes):
        core = [n for n in nodes if n["tier"] == "core"][:3]
        if core:
            return core, "core-tier nodes"
    if all("region" in n for n in nodes):
        seen, picked = set(), []
        for n in nodes:
            if n["region"] not in seen:
                seen.add(n["region"])
                picked.append(n)
        return picked, "one per region"
    return nodes[:3], "first 3 nodes"


def write_tc_rules(node: dict) -> None:
    os.makedirs(TC_DIR, exist_ok=True)
    path = os.path.join(TC_DIR, f"{node['name']}.rules")
    with open(path, "w") as f:
        for r in node["egress_rules"]:
            f.write(f"{r['dst_ip']} {r['delay_ms']} {r['jitter_ms']} "
                    f"{r['loss_pct']} {r['rate_mbit']}\n")


def main():
    with open(TOPOLOGY_FILE) as f:
        topo = json.load(f)

    nodes = topo["nodes"]
    subnet = topo["subnet"]

    bootnode_candidates, boot_desc = pick_bootnodes(nodes)
    bootnodes = ",".join(
        enode_url(n["address"], n["ip"], n["p2p_port"]) for n in bootnode_candidates
    )

    lines = []
    lines.append("networks:")
    lines.append("  gurubft-net:")
    lines.append("    driver: bridge")
    lines.append("    ipam:")
    lines.append("      config:")
    lines.append(f"        - subnet: {subnet}")
    lines.append("")
    lines.append("services:")

    for n in nodes:
        key_dir_host = os.path.join("networkFiles", "keys", n["address"])
        has_rules = "egress_rules" in n
        has_profile = "profile" in n

        lines.append(f"  {n['name']}:")
        lines.append("    build:")
        lines.append("      context: ./docker")
        lines.append(f"    container_name: {n['name']}")
        lines.append("    cap_add:")
        lines.append("      - NET_ADMIN")
        lines.append("    environment:")
        lines.append(f"      NODE_NAME: {n['name']}")
        if "tier" in n:
            lines.append(f"      NODE_TIER: {n['tier']}")
        if "region" in n:
            lines.append(f"      NODE_REGION: {n['region']}")
        if has_rules:
            write_tc_rules(n)
            lines.append("      TC_RULES_FILE: /config/tc.rules")
        elif has_profile:
            p = n["profile"]
            lines.append(f"      TC_DELAY_MS: \"{p['delay_ms']}\"")
            lines.append(f"      TC_JITTER_MS: \"{p['jitter_ms']}\"")
            lines.append(f"      TC_RATE_MBIT: \"{p['rate_mbit']}\"")
            lines.append(f"      TC_LOSS_PCT: \"{p['loss_pct']}\"")
        lines.append(f"      BOOTNODES: \"{bootnodes}\"")
        lines.append("    volumes:")
        lines.append(f"      - ./{key_dir_host}/key:/data/key.priv:ro")
        lines.append("      - ./networkFiles/genesis.json:/config/genesis.json:ro")
        if has_rules:
            lines.append(f"      - ./networkFiles/tc/{n['name']}.rules:/config/tc.rules:ro")
        lines.append(f"      - ./logs/{n['name']}:/data/logs")
        lines.append("    ports:")
        lines.append(f"      - \"{n['host_rpc_port']}:8545\"")
        lines.append(f"      - \"{n['host_ws_port']}:8546\"")
        lines.append("    networks:")
        lines.append("      gurubft-net:")
        lines.append(f"        ipv4_address: {n['ip']}")
        lines.append("    restart: unless-stopped")
        lines.append("")

    with open(OUT_FILE, "w") as f:
        f.write("\n".join(lines))

    mode = ("per-destination rules" if any("egress_rules" in n for n in nodes)
            else "per-node profiles" if any("profile" in n for n in nodes)
            else "no shaping")
    print(f"Wrote {OUT_FILE} with {len(nodes)} services ({mode}).")
    print(f"Bootnodes ({len(bootnode_candidates)}, {boot_desc}): "
          f"{[n['name'] for n in bootnode_candidates]}")


if __name__ == "__main__":
    main()