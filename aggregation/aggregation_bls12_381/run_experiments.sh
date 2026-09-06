#!/bin/bash

# ===============================
# CONFIGURATION
# ===============================

set -euo pipefail

BINARY=./target/release/aggregation_bls12_381

RUNS=10

PROOFS=(8) # 64 128 256 512 1024 2048)

# src/main.rs writes directly to output/experiment_log_<nproofs>_<run>.json
# (see the File::create(...) call near the bottom of main()) - OUTDIR must
# match that literal "output" prefix, or there's nothing for `mv` to find.
OUTDIR=output

mkdir -p "$OUTDIR"

if [[ ! -x "$BINARY" ]]; then
    echo "error: $BINARY not found or not executable - run 'cargo build --release' first." >&2
    exit 1
fi


# ===============================
# LOOP
# ===============================

echo "Starting experiments..."

for run in $(seq 1 "$RUNS")
do

    echo "================ RUN $run ================"

    for nproofs in "${PROOFS[@]}"
    do

        echo "Running: run=$run proofs=$nproofs"

        "$BINARY" "$run" "$nproofs"

        # main.rs already wrote this file straight into $OUTDIR (it writes
        # to "output/..." directly), so there's nothing left to move. This
        # check just confirms the run actually produced the file it claims
        # to have produced, instead of silently continuing like the old
        # script did after main.rs panicked.
        expected="$OUTDIR/experiment_log_${nproofs}_${run}.json"
        if [[ ! -f "$expected" ]]; then
            echo "error: expected $expected but it wasn't created - see the panic above." >&2
            exit 1
        fi

    done

done


echo "All experiments completed."