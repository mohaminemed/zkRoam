#!/usr/bin/env python3
"""
Builds docker-compose.yml from networkFiles/topology.json.

Each node gets:
  - its own volume mount exposing its keypair + shared genesis.json
  - env vars driving the tc netem profile applied by docker/entrypoint.sh
  - cap_add: NET_ADMIN (required for tc inside the container)
  - a fixed IP on the custom gurubft-net bridge network

Bootnodes: the 3 core-tier nodes are used as bootnodes for all others, since
they have the most reliable links.
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOPOLOGY_FILE = os.path.join(ROOT, "networkFiles", "topology.json")
KEYS_DIR = os.path.join(ROOT, "networkFiles", "keys")
OUT_FILE = os.path.join(ROOT, "docker-compose.yml")


def enode_url(address: str, ip: str, port: int) -> str:
    pub_path = os.path.join(KEYS_DIR, address, "key.pub")
    with open(pub_path) as f:
        pub = f.read().strip()
    # Besu writes the 04-prefixed uncompressed public key (130 hex chars incl 0x);
    # enode node-id is the 128 hex chars *without* the 04 prefix and without 0x.
    pub_hex = pub[2:] if pub.startswith("0x") else pub
    if pub_hex.startswith("04"):
        pub_hex = pub_hex[2:]
    return f"enode://{pub_hex}@{ip}:{port}"


def main():
    with open(TOPOLOGY_FILE) as f:
        topo = json.load(f)

    nodes = topo["nodes"]
    subnet = topo["subnet"]

    bootnode_candidates = [n for n in nodes if n["tier"] == "core"][:3]
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
        p = n["profile"]
        key_dir_host = os.path.join("networkFiles", "keys", n["address"])
        lines.append(f"  {n['name']}:")
        lines.append("    build:")
        lines.append("      context: ./docker")
        lines.append(f"    container_name: {n['name']}")
        lines.append("    cap_add:")
        lines.append("      - NET_ADMIN")
        lines.append("    environment:")
        lines.append(f"      NODE_NAME: {n['name']}")
        lines.append(f"      NODE_TIER: {n['tier']}")
        lines.append(f"      TC_DELAY_MS: \"{p['delay_ms']}\"")
        lines.append(f"      TC_JITTER_MS: \"{p['jitter_ms']}\"")
        lines.append(f"      TC_RATE_MBIT: \"{p['rate_mbit']}\"")
        lines.append(f"      TC_LOSS_PCT: \"{p['loss_pct']}\"")
        lines.append(f"      BOOTNODES: \"{bootnodes}\"")
        lines.append("    volumes:")
        lines.append(f"      - ./{key_dir_host}/key.priv:/data/key.priv:ro")
        lines.append("      - ./networkFiles/genesis.json:/config/genesis.json:ro")
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

    print(f"Wrote {OUT_FILE} with {len(nodes)} services.")
    print(f"Bootnodes ({len(bootnode_candidates)}): core-tier nodes "
          f"{[n['name'] for n in bootnode_candidates]}")


if __name__ == "__main__":
    main()
