#!/bin/bash

# Function to delete files or directories if they exist
delete_if_exists() {
    if [ -e "$1" ]; then
        echo "Deleting $1..."
        rm -rf "$1"
    else
        echo "$1 does not exist. Skipping..."
    fi
}

delete_if_exists "CDRGeneration_cpp"
delete_if_exists "CDRGeneration_js"
delete_if_exists "zkMetrics"

delete_if_exists "circuit_compilation_output.txt"
delete_if_exists "proving_time_comparison.png"
delete_if_exists "memory_usage_comparison.png"
delete_if_exists "proof.json"
delete_if_exists "public.json"
delete_if_exists "verification_key.json"
delete_if_exists "witness.wtns"

for file in CDRGeneration_final*; do
    delete_if_exists "$file"
done

for file in CDRGeneration*; do
    delete_if_exists "$file"
done

for file in c*; do
    if [ -f "$file" ]; then  # Only delete files, not directories
        delete_if_exists "$file"
    fi
done

for file in pot*; do
    delete_if_exists "$file"
done

for file in response*; do
    delete_if_exists "$file"
done

echo "Cleanup completed!"