#!/bin/bash
# Crawl every enabled pincode in saved chunks, then print the merge command.
#
#   bash scripts/crawl_chunks.sh              # start a new crawl
#   BASE=2026-09-26T0500Z bash scripts/...    # resume one: finished chunks are skipped
#
# Why chunks: a crawl of all 24 pincodes takes hours and scrape.py only writes its snapshot at the
# very end, so anything that kills it loses everything. Each chunk writes its own snapshot; the parts
# are folded into one run afterwards with db.merge_runs.
#
# Run this in a normal Terminal, not inside an agent/CI shell with a memory watchdog: three attempts
# on 2026-09-23 were killed part-way as background tasks.
set -u
cd "$(dirname "$0")/.." || exit 1
LOG="$(pwd)/logs/chunked_crawl.log"
BASE="${BASE:-$(date -u +%Y-%m-%dT%H%MZ)}"
CHUNKS=("400001,400050,400053" "400076,110001,110016" "110085,122002,560001" "560034,560038,560066"
        "600004,600020,600040" "600096,500034,500081" "500072,500016,700019" "700029,700091,700157")
GAP="${GAP:-240}"   # seconds between chunks; Blinkit truncates collections when pushed hard
mkdir -p logs data/snapshots
echo "BASE=$BASE  chunks=${#CHUNKS[@]}  log=$LOG" | tee -a "$LOG"
i=0; parts=()
for ch in "${CHUNKS[@]}"; do
  i=$((i+1)); rid="$BASE"; [ "$i" -gt 1 ] && rid="$BASE-part$i"
  [ "$i" -gt 1 ] && parts+=("$rid")
  if [ -f "data/snapshots/$rid.parquet" ]; then echo "chunk $i/${#CHUNKS[@]} already saved, skipping" | tee -a "$LOG"; continue; fi
  echo "chunk $i/${#CHUNKS[@]} START $rid ($ch) at $(date +%H:%M:%S)" | tee -a "$LOG"
  caffeinate -i python3 scrape.py --pincodes "$ch" --run-id "$rid" --max-pages 80 --network-wait 1800 \
      --page-delay 1.5,3 --group-delay 6,12 --pincode-delay 30,60 --no-load >> "$LOG" 2>&1
  if [ ! -f "data/snapshots/$rid.parquet" ]; then
    echo "chunk $i/${#CHUNKS[@]} FAILED — see $LOG. Re-run with BASE=$BASE to continue." | tee -a "$LOG"; exit 3
  fi
  echo "chunk $i/${#CHUNKS[@]} SAVED at $(date +%H:%M:%S)" | tee -a "$LOG"
  [ "$i" -lt "${#CHUNKS[@]}" ] && sleep "$GAP"
done
echo "ALL CHUNKS DONE base=$BASE" | tee -a "$LOG"
echo "next: python -m db.merge_runs $BASE ${parts[*]} --dry-run" | tee -a "$LOG"
