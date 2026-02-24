#!/bin/bash

# ================================
# Clique PoA Private Network Setup
# ================================

# Network parameters
NETWORK_ID=12345
CHAIN_DIR=$(pwd)
BOOTNODE_KEY=$CHAIN_DIR/boot.key

# Node directories
NODE1=$CHAIN_DIR/node1
NODE2=$CHAIN_DIR/node2
NODE3=$CHAIN_DIR/node3
NODE4=$CHAIN_DIR/node4

# Genesis file
GENESIS=$CHAIN_DIR/genesis.json

# Password files
PWD1=$NODE1/password.txt
PWD2=$NODE2/password.txt
PWD3=$NODE3/password.txt
PWD4=$NODE4/password.txt

# Miner addresses
MINER1=0x73a7245EFcAeb3Addf55a55afFc75A956b69854c
MINER2=0x0Ea0Eb8061cBdaF6684852A583234d882dA63d25
MINER3=0x58D85998a7c6ed077f9FB913700f5f5Da539a786
MINER4=0x46CC7efbC0fb7F80c037B33c7fe416692Ea1075B

# ================================
# 1️⃣ Initialize Nodes
# ================================
geth init --datadir $NODE1 $GENESIS
geth init --datadir $NODE2 $GENESIS
geth init --datadir $NODE3 $GENESIS
geth init --datadir $NODE4 $GENESIS



BOOTNODE_URL="enode://446b7497621cf30eba72647b5ec798b2870f0f9623136608229d87400dfc58295211a72c0de0d2523adc75be1a4a72e6bec82d3a759089067d7764bbf0abc7a0@127.0.0.1:30305"
echo "Bootnode enode URL: $BOOTNODE_URL"

# Then start Node 2,3,4 using $BOOTNODE_URL


# ================================
# 3️⃣ Start Node 2
# ================================
gnome-terminal --title="Node 2" -- bash -c \
"geth --datadir $NODE2 \
     --networkid $NETWORK_ID \
     --bootnodes $BOOTNODE_URL \
     --unlock $MINER2 --password $PWD2 \
     --allow-insecure-unlock \
     --http --http.addr 0.0.0.0 --http.port 8546 --http.api eth,net,web3,personal \
     --ws --ws.addr 0.0.0.0 --ws.port 9001 --ws.api eth,net,web3 \
     --port 30306 \
     --authrpc.port 8552
     --syncmode full --verbosity 3; exec bash"

# ================================
# 4️⃣ Start Node 3
# ================================
gnome-terminal --title="Node 3" -- bash -c \
"geth --datadir $NODE3 \
     --networkid $NETWORK_ID \
     --bootnodes $BOOTNODE_URL \
     --unlock $MINER3 --password $PWD3 \
     --allow-insecure-unlock \
     --http --http.addr 0.0.0.0 --http.port 8547 --http.api eth,net,web3,personal \
     --ws --ws.addr 0.0.0.0 --ws.port 9002 --ws.api eth,net,web3 \
     --port 30307 \
     --authrpc.port 8553
     --syncmode full --verbosity 3; exec bash"

# ================================
# 5️⃣ Start Node 4
# ================================
gnome-terminal --title="Node 4" -- bash -c \
"geth --datadir $NODE4 \
     --networkid $NETWORK_ID \
     --bootnodes $BOOTNODE_URL \
     --unlock $MINER4 --password $PWD4 \
     --allow-insecure-unlock \
     --http --http.addr 0.0.0.0 --http.port 8548 --http.api eth,net,web3,personal \
     --ws --ws.addr 0.0.0.0 --ws.port 9003 --ws.api eth,net,web3 \
     --port 30308 \
     --authrpc.port 8554
     --syncmode full --verbosity 3; exec bash"

echo "✅ All nodes started successfully!"