#!/bin/bash
# ==========================================
# Generate multiple Groth16 proofs from a single witness
# and measure off-chain proof generation & verification performance
# ==========================================

# -------------------------------
# Config
# -------------------------------
ZKEY="CDRGeneration_final.zkey"       # Your compiled zkey
WITNESS="witness.wtns"               # Single witness file to reuse
PROOF_DIR="zkProofs"                 # Directory to save proofs
PUBLIC_DIR="zkPublic"                 # Directory to save public inputs
N_PROOFS=8                            # Number of times to generate proofs

# Verification key
VK="verification_key.json"            # Verification key file

# -------------------------------
# Create output directories if they don't exist
# -------------------------------
mkdir -p "$PROOF_DIR"
mkdir -p "$PUBLIC_DIR"

# -------------------------------
# Clean previous metrics
# -------------------------------
METRICS_FILE="$PROOF_DIR/performance_metrics.csv"
> "$METRICS_FILE"
echo "ProofID,Timestamp,ProofElapsedTime(s),ProofMemory(MB),VerifyTime(s),VerifyMemory(MB)" >> "$METRICS_FILE"

# -------------------------------
# Loop to generate multiple proofs from the same witness
# -------------------------------
for i in $(seq 1 $N_PROOFS); do
    base="witness_$i"
    timestamp=$(date +"%Y-%m-%d %H:%M:%S")

    echo "Generating proof #$i ..."

    # -------------------------------
    # Generate proof and measure time + memory
    # -------------------------------
    proof_metrics=$( /usr/bin/time -f "Elapsed Time: %e seconds\nMaximum Memory: %M KB" \
        snarkjs groth16 prove "$ZKEY" "$WITNESS" "$PROOF_DIR/${base}_proof.json" "$PUBLIC_DIR/${base}_public.json" 2>&1 )

    # Extract proof metrics
    proof_elapsed=$(echo "$proof_metrics" | grep "Elapsed Time" | awk '{print $3}')
    proof_mem_kb=$(echo "$proof_metrics" | grep "Maximum Memory" | awk '{print $3}')
    proof_mem=$(awk "BEGIN {printf \"%.2f\", $proof_mem_kb/1024}")  # Convert KB -> MB

    # -------------------------------
    # Verify proof and measure time + memory
    # -------------------------------
    verify_metrics=$( /usr/bin/time -f "Elapsed Time: %e seconds\nMaximum Memory: %M KB" \
        snarkjs groth16 verify "$VK" "$PUBLIC_DIR/${base}_public.json" "$PROOF_DIR/${base}_proof.json" 2>&1 )

    # Extract verification metrics
    verify_elapsed=$(echo "$verify_metrics" | grep "Elapsed Time" | awk '{print $3}')
    verify_mem_kb=$(echo "$verify_metrics" | grep "Maximum Memory" | awk '{print $3}')
    verify_mem=$(awk "BEGIN {printf \"%.2f\", $verify_mem_kb/1024}")  # Convert KB -> MB

    # -------------------------------
    # Save metrics to CSV
    # -------------------------------
    echo "$base,$timestamp,$proof_elapsed,$proof_mem,$verify_elapsed,$verify_mem" >> "$METRICS_FILE"

    # -------------------------------
    # Print info to console
    # -------------------------------
    echo "✅ Proof #$i saved: $PROOF_DIR/${base}_proof.json"
    echo "   Proof time: $proof_elapsed s | Memory: $proof_mem MB"
    echo "   Verify time: $verify_elapsed s | Memory: $verify_mem MB"
    echo "----------------------------------------"
done

echo "All $N_PROOFS proofs generated from the same witness."
echo "Performance metrics stored in $METRICS_FILE"