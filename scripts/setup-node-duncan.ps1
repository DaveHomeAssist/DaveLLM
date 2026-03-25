# DaveLLM Node Setup — Duncan Workstation (RX 5700 XT, 80 GB RAM)
# Role: Large model via CPU inference (70B quality node)
# Run this script in PowerShell as Administrator
#
# NOTE: Duncan uses CPU inference (80 GB RAM) because RX 5700 XT (RDNA 1)
# has limited llama.cpp GPU support. Ollama will automatically use CPU
# when the model doesn't fit in VRAM. With 80 GB RAM, a 70B Q4 model
# (~40 GB) loads comfortably.

Write-Host "=== DaveLLM Node Setup: Duncan (80 GB RAM, CPU Inference) ===" -ForegroundColor Cyan

# 1. Install Ollama
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    Write-Host "Installing Ollama..." -ForegroundColor Yellow
    winget install Ollama.Ollama --accept-package-agreements --accept-source-agreements
} else {
    Write-Host "Ollama already installed." -ForegroundColor Green
}

# 2. Set Ollama to listen on all interfaces
[System.Environment]::SetEnvironmentVariable("OLLAMA_HOST", "0.0.0.0", "User")
Write-Host "Set OLLAMA_HOST=0.0.0.0 (LAN accessible)" -ForegroundColor Green

# 3. Pull the assigned model (large — this will take a while)
Write-Host "Pulling llama3.1:70b-instruct-q4_K_M (quality model, ~40 GB)..." -ForegroundColor Yellow
Write-Host "This will take 15-30 minutes depending on your connection." -ForegroundColor Yellow
ollama pull llama3.1:70b-instruct-q4_K_M

# 4. Add Windows Firewall rule for port 11434
$ruleName = "DaveLLM-Ollama"
$existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if (-not $existing) {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP -LocalPort 11434 -Action Allow
    Write-Host "Firewall rule added for port 11434." -ForegroundColor Green
} else {
    Write-Host "Firewall rule already exists." -ForegroundColor Green
}

# 5. Verify
Write-Host "`n=== Verification ===" -ForegroundColor Cyan
ollama list
Write-Host "`nNode ready. Ollama serving on port 11434." -ForegroundColor Green
Write-Host "Test from Mac: curl http://<THIS_IP>:11434/api/tags" -ForegroundColor Yellow
Write-Host "`nNOTE: First inference will be slow (~30s load time for 70B model)." -ForegroundColor Yellow
