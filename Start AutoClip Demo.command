#!/bin/zsh
set -e
cd "$(dirname "$0")"
python3 -m autoclip_batch --simulate --demo-failure
echo
echo "Demo complete. Run again to see the hash cache skip completed work."
read -k 1 "?Press any key to close..."
