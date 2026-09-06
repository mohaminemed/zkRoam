#!/bin/bash

LOGFILE="pot_time_log.txt"
echo "Powers of Tau Timing Log - $(date)" > $LOGFILE
echo "-----------------------------------" >> $LOGFILE

# Function to measure time
measure_time () {
    STEP_NAME=$1
    shift

    echo "$STEP_NAME..."
    START=$(date +%s.%N)

    "$@"

    END=$(date +%s.%N)
    ELAPSED=$(echo "$END - $START" | bc)

    echo "$STEP_NAME completed in $ELAPSED seconds"
    echo "$STEP_NAME: $ELAPSED seconds" >> $LOGFILE
}

TOTAL_START=$(date +%s.%N)

# Step 1
measure_time "Step 1: Creating initial Powers of Tau file" \
snarkjs powersoftau new bn128 14 pot14_0000.ptau -v

# Step 2
measure_time "Step 2: First contribution" \
bash -c 'echo -e "blabla" | snarkjs powersoftau contribute pot14_0000.ptau pot14_0001.ptau --name="First contribution" -v'

# Step 3
measure_time "Step 3: Second contribution" \
snarkjs powersoftau contribute pot14_0001.ptau pot14_0002.ptau --name="Second contribution" -v -e="some random text"

# Step 4
measure_time "Step 4: Exporting challenge" \
snarkjs powersoftau export challenge pot14_0002.ptau challenge_0003

# Step 5
measure_time "Step 5: Contributing to the challenge" \
snarkjs powersoftau challenge contribute bn128 challenge_0003 response_0003 -e="some random text"

# Step 6
measure_time "Step 6: Importing response" \
snarkjs powersoftau import response pot14_0002.ptau response_0003 pot14_0003.ptau -n="Third contribution name"

# Step 7 Verify
echo "Step 7: Verifying Powers of Tau file..."
VERIFY_START=$(date +%s.%N)

snarkjs powersoftau verify pot14_0003.ptau

VERIFY_END=$(date +%s.%N)
VERIFY_TIME=$(echo "$VERIFY_END - $VERIFY_START" | bc)

echo "Step 7 completed in $VERIFY_TIME seconds"
echo "Step 7: Verification: $VERIFY_TIME seconds" >> $LOGFILE

if [ $? -ne 0 ]; then
    echo "Verification failed. Exiting."
    exit 1
fi

# Step 8
measure_time "Step 8: Applying the beacon" \
snarkjs powersoftau beacon pot14_0003.ptau pot14_beacon.ptau \
0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f \
10 -n="Final Beacon"

# Step 9
measure_time "Step 9: Preparing phase 2" \
snarkjs powersoftau prepare phase2 pot14_beacon.ptau pot14_final.ptau -v

TOTAL_END=$(date +%s.%N)
TOTAL_TIME=$(echo "$TOTAL_END - $TOTAL_START" | bc)

echo "-----------------------------------" >> $LOGFILE
echo "Total Time: $TOTAL_TIME seconds" >> $LOGFILE

echo ""
echo "Pre-processing completed successfully!"
echo "Final file: pot14_final.ptau"
echo "Total Time: $TOTAL_TIME seconds"
echo "Timing log saved in $LOGFILE"