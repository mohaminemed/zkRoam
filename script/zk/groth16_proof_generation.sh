#!/bin/bash

delete_if_exists() {
    if [ -e "$1" ]; then
        echo "Deleting $1..."
        rm -rf "$1"
    else
        echo "$1 does not exist. Skipping..."
    fi
}

# Create directories if they don't exist
mkdir -p zkMetrics/groth16

# Compile the Circom circuit
echo "Step 1: Compiling the Circom circuit..."
circom --r1cs --wasm --c --sym --inspect circuits/circom/CDRGeneration.circom > zkMetrics/circuit_compilation_output.txt 2>&1

# Check if compilation succeeded
if [ $? -ne 0 ]; then
    echo "Circuit compilation failed. Check zkMetrics/circuit_compilation_output.txt for details."
    exit 1
fi
echo "Circuit compilation completed. Output saved to zkMetrics/circuit_compilation_output.txt."

#  Generate the witness
echo "Step 2: Generating the witness..."
snarkjs wtns calculate CDRGeneration_js/CDRGeneration.wasm input.json witness.wtns

# Check if witness generation succeeded
if [ $? -ne 0 ]; then
    echo "Witness generation failed."
    exit 1
fi
echo "Witness generation completed."

# Set up Groth16 (initial zkey)
echo "Step 3: Setting up Groth16 (initial zkey)..."
snarkjs groth16 setup CDRGeneration.r1cs pot14_final.ptau CDRGeneration_0000.zkey

# Check if setup succeeded
if [ $? -ne 0 ]; then
    echo "Groth16 setup failed."
    exit 1
fi
echo "Groth16 setup completed."

# First contribution
echo "Step 4: First contribution..."
echo -e "blabla" | snarkjs zkey contribute CDRGeneration_0000.zkey CDRGeneration_0001.zkey --name="1st Contributor Name" -v

# Check if contribution succeeded
if [ $? -ne 0 ]; then
    echo "First contribution failed."
    exit 1
fi
echo "First contribution completed."

# Second contribution
echo "Step 5: Second contribution..."
snarkjs zkey contribute CDRGeneration_0001.zkey CDRGeneration_0002.zkey --name="Second contribution Name" -v -e="Another random entropy"

# Check if contribution succeeded
if [ $? -ne 0 ]; then
    echo "Second contribution failed."
    exit 1
fi
echo "Second contribution completed."

# Export Bellman challenge
echo "Step 6: Exporting Bellman challenge..."
snarkjs zkey export bellman CDRGeneration_0002.zkey challenge_phase2_0003

# Check if export succeeded
if [ $? -ne 0 ]; then
    echo "Bellman challenge export failed."
    exit 1
fi
echo "Bellman challenge exported."

# Contribute to Bellman challenge
echo "Step 7: Contributing to Bellman challenge..."
snarkjs zkey bellman contribute bn128 challenge_phase2_0003 response_phase2_0003 -e="some random text"

# Check if contribution succeeded
if [ $? -ne 0 ]; then
    echo "Bellman challenge contribution failed."
    exit 1
fi
echo "Bellman challenge contribution completed."

# Import Bellman response
echo "Step 8: Importing Bellman response..."
snarkjs zkey import bellman CDRGeneration_0002.zkey response_phase2_0003 CDRGeneration_0003.zkey -n="Third contribution name"

# Check if import succeeded
if [ $? -ne 0 ]; then
    echo "Bellman response import failed."
    exit 1
fi
echo "Bellman response imported."

# Verify the zkey
echo "Step 9: Verifying the zkey..."
snarkjs zkey verify CDRGeneration.r1cs pot14_final.ptau CDRGeneration_0003.zkey

# Check if verification succeeded
if [ $? -ne 0 ]; then
    echo "ZKey verification failed."
    exit 1
fi
echo "ZKey verification completed."

# Apply the beacon
echo "Step 10: Applying the beacon..."
snarkjs zkey beacon CDRGeneration_0003.zkey CDRGeneration_final.zkey 0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f 10 -n="Final Beacon"

# Check if beacon application succeeded
if [ $? -ne 0 ]; then
    echo "Beacon application failed."
    exit 1
fi
echo "Beacon application completed."

# Verify the final zkey
echo "Step 11: Verifying the final zkey..."
snarkjs zkey verify CDRGeneration.r1cs pot14_final.ptau CDRGeneration_final.zkey

# Check if verification succeeded
if [ $? -ne 0 ]; then
    echo "Final ZKey verification failed."
    exit 1
fi
echo "Final ZKey verification completed."

# Export the verification key
echo "Step 12: Exporting the verification key..."
snarkjs zkey export verificationkey CDRGeneration_final.zkey verification_key.json

# Check if export succeeded
if [ $? -ne 0 ]; then
    echo "Verification key export failed."
    exit 1
fi
echo "Verification key exported to verification_key.json."

# TRUSTED SETUP DONE, GENERATING PROOF

echo "Generating Solidity Verifier..."
delete_if_exists "src/Groth16Verifier.sol"
snarkjs zkey export solidityverifier CDRGeneration_final.zkey src/Groth16Verifier.sol

# echo "Step 13: Generating the proof and benchmarking performance..."
# /usr/bin/time -f "Elapsed Time: %e seconds\nMaximum Memory: %M KB" snarkjs groth16 prove CDRGeneration_final.zkey witness.wtns proof.json public.json 2> zkMetrics/groth16/time_output.txt

# # Extract proving time and memory usage
# elapsed_time=$(grep "Elapsed Time" zkMetrics/groth16/time_output.txt | awk '{print $3}')
# memory_usage=$(grep "Maximum Memory" zkMetrics/groth16/time_output.txt | awk '{print $3}')

# # Save performance metrics to a CSV file
# echo "ElapsedTime,MemoryUsage" > zkMetrics/groth16/performance_metrics.csv
# echo "$elapsed_time,$memory_usage" >> zkMetrics/groth16/performance_metrics.csv

# echo "Proof generation completed. Performance metrics saved to zkMetrics/groth16/performance_metrics.csv."
# echo "Elapsed Time: $elapsed_time seconds"
# echo "Maximum Memory: $memory_usage KB"