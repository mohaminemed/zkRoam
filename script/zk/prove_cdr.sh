echo "Generating Solidity Verifier..."
snarkjs zkey export solidityverifier CDRGeneration_final.zkey src/Groth16Verifier.sol

# echo "Step 13: Generating the proof and benchmarking performance..."
 /usr/bin/time -f "Elapsed Time: %e seconds\nMaximum Memory: %M KB" snarkjs groth16 prove CDRGeneration_final.zkey witness.wtns proof.json public.json 2> zkMetrics/groth16/time_output.txt

# # Extract proving time and memory usage
 elapsed_time=$(grep "Elapsed Time" zkMetrics/groth16/time_output.txt | awk '{print $3}')
 memory_usage=$(grep "Maximum Memory" zkMetrics/groth16/time_output.txt | awk '{print $3}')

# # Save performance metrics to a CSV file
 echo "ElapsedTime,MemoryUsage" > zkMetrics/groth16/performance_metrics.csv
 echo "$elapsed_time,$memory_usage" >> zkMetrics/groth16/performance_metrics.csv

# echo "Proof generation completed. Performance metrics saved to zkMetrics/groth16/performance_metrics.csv."
 echo "Elapsed Time: $elapsed_time seconds"
 echo "Maximum Memory: $memory_usage KB"