#!/bin/bash
# DaveLLM Cluster Health Check — run from Mac
# Usage: ./check-cluster.sh

echo "=== DaveLLM Cluster Health Check ==="
echo ""

# Update these with your actual LAN IPs
NODES=(
    "node-gp66|PLACEHOLDER_GP66_IP"
    "node-katana-1|PLACEHOLDER_KATANA1_IP"
    "node-katana-2|PLACEHOLDER_KATANA2_IP"
    "node-duncan|PLACEHOLDER_DUNCAN_IP"
)

HEALTHY=0
TOTAL=${#NODES[@]}

for entry in "${NODES[@]}"; do
    IFS='|' read -r name ip <<< "$entry"
    printf "%-20s %s  " "$name" "$ip"

    # Check if Ollama is responding
    response=$(curl -s --connect-timeout 3 "http://$ip:11434/api/tags" 2>/dev/null)
    if [ $? -eq 0 ] && [ -n "$response" ]; then
        models=$(echo "$response" | python3 -c "import sys,json; d=json.load(sys.stdin); print(', '.join(m['name'] for m in d.get('models',[])))" 2>/dev/null)
        echo "ONLINE  [$models]"
        HEALTHY=$((HEALTHY + 1))
    else
        echo "OFFLINE"
    fi
done

echo ""
echo "=== $HEALTHY/$TOTAL nodes healthy ==="

# Check local Ollama fallback
printf "%-20s %s  " "mac-fallback" "127.0.0.1"
local_resp=$(curl -s --connect-timeout 2 "http://127.0.0.1:11434/api/tags" 2>/dev/null)
if [ $? -eq 0 ] && [ -n "$local_resp" ]; then
    local_models=$(echo "$local_resp" | python3 -c "import sys,json; d=json.load(sys.stdin); print(', '.join(m['name'] for m in d.get('models',[])))" 2>/dev/null)
    echo "ONLINE  [$local_models]"
else
    echo "OFFLINE"
fi

echo ""

# Check DaveLLM router
printf "%-20s %s  " "davellm-router" "127.0.0.1:8000"
router_resp=$(curl -s --connect-timeout 2 "http://127.0.0.1:8000/health" 2>/dev/null)
if [ $? -eq 0 ] && [ -n "$router_resp" ]; then
    echo "ONLINE"
else
    echo "OFFLINE"
fi
