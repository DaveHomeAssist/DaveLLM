#!/bin/bash
# DaveLLM Cluster Verification Script
# Run from the Mac to verify all nodes are reachable and serving models.
#
# Usage: ./verify-cluster.sh
# Or with custom IPs: GP66_IP=x.x.x.x KATANA1_IP=x.x.x.x ... ./verify-cluster.sh

GP66_IP="${GP66_IP:-PLACEHOLDER_GP66_IP}"
KATANA1_IP="${KATANA1_IP:-PLACEHOLDER_KATANA1_IP}"
KATANA2_IP="${KATANA2_IP:-PLACEHOLDER_KATANA2_IP}"
DUNCAN_IP="${DUNCAN_IP:-PLACEHOLDER_DUNCAN_IP}"

NODES=("node-gp66:$GP66_IP" "node-katana-1:$KATANA1_IP" "node-katana-2:$KATANA2_IP" "node-duncan:$DUNCAN_IP")

echo "=== DaveLLM Cluster Verification ==="
echo ""

PASS=0
FAIL=0

for entry in "${NODES[@]}"; do
    name="${entry%%:*}"
    ip="${entry##*:}"

    printf "%-20s %-16s " "$name" "$ip"

    if [[ "$ip" == PLACEHOLDER* ]]; then
        echo "⏭  SKIPPED (no IP set)"
        continue
    fi

    result=$(curl -s --connect-timeout 3 "http://$ip:11434/api/tags" 2>/dev/null)

    if [ $? -eq 0 ] && [ -n "$result" ]; then
        model_count=$(echo "$result" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('models',[])))" 2>/dev/null || echo "?")
        echo "✅ ONLINE ($model_count models)"
        ((PASS++))
    else
        echo "❌ OFFLINE"
        ((FAIL++))
    fi
done

echo ""
echo "=== Results: $PASS online, $FAIL offline ==="

# Check local Ollama fallback
printf "%-20s %-16s " "mac-fallback" "localhost"
if curl -s --connect-timeout 2 "http://localhost:11434/api/tags" > /dev/null 2>&1; then
    echo "✅ ONLINE (local fallback)"
else
    echo "⚠️  NOT RUNNING (start with: ollama serve)"
fi

echo ""
echo "Once all nodes show ONLINE, start the router:"
echo "  cd dave-llm && uvicorn app:app --host 0.0.0.0 --port 8000"
