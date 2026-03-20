#!/bin/bash
# Permanent fix for flash-agent CronJob concurrency policy
# Usage: ./apply-flash-agent-fix.sh [namespace] [chart-path]

NAMESPACE=${1:-sock-shop}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART_PATH=${2:-"$(dirname "$SCRIPT_DIR")/charts/flash-agent"}

echo '[FIX] Upgrading flash-agent Helm release with patched chart...'
echo "      Namespace: $NAMESPACE"
echo "      Chart: $CHART_PATH"

if [ ! -d "$CHART_PATH" ]; then
  echo "ERROR: Chart path not found: $CHART_PATH"
  exit 1
fi

helm upgrade flash-agent "$CHART_PATH" -n "$NAMESPACE" --install

echo ''
echo '[FIX] Verifying CronJob configuration...'
kubectl get cronjob -n "$NAMESPACE" flash-agent-cronjob -o yaml | grep -E 'concurrencyPolicy|successfulJobsHistoryLimit|failedJobsHistoryLimit'

echo ''
echo '[FIX] ✓ Flash-agent is now configured with:'
echo '      - concurrencyPolicy: Forbid (prevents overlapping runs)'
echo '      - successfulJobsHistoryLimit: 1 (keeps only latest successful job)'
echo '      - failedJobsHistoryLimit: 1 (keeps only latest failed job)'
echo ''
echo '[FIX] This prevents CronJob pods from stacking up and consuming memory.'
