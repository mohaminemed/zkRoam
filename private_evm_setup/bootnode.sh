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

# Start Node 1 as bootnode + signer in background
geth --datadir $NODE1 \
     --networkid $NETWORK_ID \
     --unlock $MINER1 --password $PWD1 \
     --allow-insecure-unlock \
     --http --http.addr 0.0.0.0 --http.port 8545 --http.api eth,net,web3,personal,clique,miner \
     --ws --ws.addr 0.0.0.0 --ws.port 9000 --ws.api eth,net,web3,personal,clique,miner \
     --nodiscover=false --port 30305 \
     --http.corsdomain '*' --miner.etherbase 0x73a7245EFcAeb3Addf55a55afFc75A956b69854c --mine --syncmode full --miner.gaslimit 1000000000 --rpc.gascap 1000000000 \
     --syncmode full --verbosity 3 > node1.log 2>&1 


