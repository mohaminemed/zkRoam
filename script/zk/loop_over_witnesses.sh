#!/bin/bash
# ================================
# Generate multiple Groth16 proofs from a single witness
# and measure off-chain verification time
# ================================

# Config
ZKEY="CDRGeneration_final.zkey"       # Your compiled zkey
WITNESS="witness.wtns"               # Single witness file to reuse
PROOF_DIR="zkProofs"                 # Directory to save proofs
PUBLIC_DIR="zkPublic"                 # Directory to save public inputs
N_PROOFS=8                            # Number of times to generate proofs

# Create output directories if they don't exist
mkdir -p $PROOF_DIR
mkdir -p $PUBLIC_DIR

# Clean previous metrics
> $PROOF_DIR/performance_metrics.csv
echo "ProofID,ElapsedTime,MemoryUsage,VerifyTime" >> $PROOF_DIR/performance_metrics.csv

# Loop to generate multiple proofs from the same witness
for i in $(seq 1 $N_PROOFS); do
    base="witness_$i"

    echo "Generating proof #$i ..."

    # Generate proof
    /usr/bin/time -f "Elapsed Time: %e seconds\nMaximum Memory: %M KB" \
        snarkjs groth16 prove $ZKEY $WITNESS $PROOF_DIR/${base}_proof.json $PUBLIC_DIR/${base}_public.json 2> $PROOF_DIR/${base}_time.txt

    # Extract performance metrics
    elapsed_time=$(grep "Elapsed Time" $PROOF_DIR/${base}_time.txt | awk '{print $3}')
    memory_usage=$(grep "Maximum Memory" $PROOF_DIR/${base}_time.txt | awk '{print $3}')

    # -------------------------------
    # Measure verification time
    # -------------------------------
    verify_time=$( { /usr/bin/time -f "%e" snarkjs groth16 verify verification_key.json $PUBLIC_DIR/${base}_public.json $PROOF_DIR/${base}_proof.json; } 2>&1 )    
    # Save metrics including verification
    echo "$base,$elapsed_time,$memory_usage,$verify_time" >> $PROOF_DIR/performance_metrics.csv

    echo "✅ Proof #$i saved: $PROOF_DIR/${base}_proof.json"
    echo "   Verification time: $verify_time s"
done

echo "All $N_PROOFS proofs generated from the same witness."
echo "Performance metrics stored in $PROOF_DIR/performance_metrics.csv"