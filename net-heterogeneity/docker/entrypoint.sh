#!/usr/bin/env bash
# Shaping mode is chosen by what is set:
#   WAN mode  (TC_RULES_FILE set): per-destination htb + netem + u32 filters
#   Tier mode (TC_DELAY_MS set):   single netem + tbf profile
#   Neither:                       no shaping
# Always required: NODE_NAME, BOOTNODES
set -euo pipefail
trap 'echo "[${NODE_NAME:-?}] FAILED line ${LINENO}: ${BASH_COMMAND}" >&2' ERR

IFACE="eth0"
NODE_TIER="${NODE_TIER:-n/a}"
NODE_REGION="${NODE_REGION:-n/a}"
TC_RULES_FILE="${TC_RULES_FILE:-}"
TC_DELAY_MS="${TC_DELAY_MS:-}"

if [ -n "${TC_RULES_FILE}" ] && [ -f "${TC_RULES_FILE}" ]; then
  echo "[${NODE_NAME}] region=${NODE_REGION} applying per-destination WAN shaping from ${TC_RULES_FILE}"

  tc qdisc add dev "${IFACE}" root handle 1: htb default 1
  tc class add dev "${IFACE}" parent 1: classid 1:1 htb rate 1000mbit

  N=10
  while read -r dst_ip delay jitter loss rate; do
    [ -z "${dst_ip}" ] && continue
    tc class add dev "${IFACE}" parent 1: classid 1:${N} htb rate "${rate}mbit"

    if awk -v j="${jitter}" 'BEGIN { exit !(j > 0) }'; then
      DELAY_CLAUSE="delay ${delay}ms ${jitter}ms distribution normal"
    else
      DELAY_CLAUSE="delay ${delay}ms"
    fi

    tc qdisc add dev "${IFACE}" parent 1:${N} handle $((100 + N)): netem \
        ${DELAY_CLAUSE} loss "${loss}%"
    tc filter add dev "${IFACE}" protocol ip parent 1: prio 1 u32 \
        match ip dst "${dst_ip}/32" flowid 1:${N}
    N=$((N + 1))
  done < "${TC_RULES_FILE}"

  echo "[${NODE_NAME}] tc rules applied:"
  tc qdisc show dev "${IFACE}"

elif [ -n "${TC_DELAY_MS}" ]; then
  TC_JITTER_MS="${TC_JITTER_MS:-0}"
  TC_RATE_MBIT="${TC_RATE_MBIT:-1000}"
  TC_LOSS_PCT="${TC_LOSS_PCT:-0}"

  echo "[${NODE_NAME}] tier=${NODE_TIER} applying tc netem: " \
       "delay=${TC_DELAY_MS}ms(+/-${TC_JITTER_MS}ms) rate=${TC_RATE_MBIT}mbit loss=${TC_LOSS_PCT}%"

  if [ "${TC_JITTER_MS}" -gt 0 ]; then
    DELAY_CLAUSE="delay ${TC_DELAY_MS}ms ${TC_JITTER_MS}ms distribution normal"
  else
    DELAY_CLAUSE="delay ${TC_DELAY_MS}ms"
  fi

  tc qdisc add dev "${IFACE}" root handle 1: netem \
      ${DELAY_CLAUSE} \
      loss ${TC_LOSS_PCT}%
  tc qdisc add dev "${IFACE}" parent 1: handle 2: tbf \
      rate ${TC_RATE_MBIT}mbit burst 32kbit latency 400ms

  echo "[${NODE_NAME}] tc rules applied:"
  tc qdisc show dev "${IFACE}"
else
  echo "[${NODE_NAME}] no tc shaping configured"
fi

mkdir -p /data/logs

besu \
  --data-path=/data \
  --genesis-file=/config/genesis.json \
  --node-private-key-file=/data/key.priv \
  --p2p-host=0.0.0.0 \
  --p2p-port=30303 \
  --rpc-http-enabled \
  --rpc-http-host=0.0.0.0 \
  --rpc-http-port=8545 \
  --rpc-http-api=ETH,QBFT,NET,ADMIN,WEB3 \
  --rpc-ws-enabled \
  --rpc-ws-host=0.0.0.0 \
  --rpc-ws-port=8546 \
  --host-allowlist="*" \
  --rpc-http-cors-origins="*" \
  --min-gas-price=0 \
  --logging=INFO \
  --bootnodes="${BOOTNODES}" \
  2>&1 | tee "/data/logs/${NODE_NAME}.log"
