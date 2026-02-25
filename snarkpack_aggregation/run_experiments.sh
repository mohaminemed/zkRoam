#!/bin/bash

# ===============================
# CONFIGURATION
# ===============================

BINARY=./target/release/snarkpack_aggregation

RUNS=10

PROOFS=(64 128 256 512 1024 2048)

OUTDIR=results

mkdir -p $OUTDIR


# ===============================
# LOOP
# ===============================

echo "Starting experiments..."

for run in $(seq 1 $RUNS)
do

    echo "================ RUN $run ================"

    for nproofs in "${PROOFS[@]}"
    do

        echo "Running: run=$run proofs=$nproofs"

        $BINARY $run $nproofs

        # move generated file to results folder
        mv experiment_log_${nproofs}_${run}.json \
           $OUTDIR/

    done

done


echo "All experiments completed."