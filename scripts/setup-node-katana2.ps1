# DaveLLM Node Setup — MSI Katana #2 (RTX 4060)
# Role: Vision / multimodal model
# Run this script in PowerShell as Administrator

Write-Host "=== DaveLLM Node Setup: Katana 2 (RTX 4060) ===" -ForegroundColor Cyan

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

# 3. Pull the assigned model
Write-Host "Pulling llava:13b (vision/multimodal model)..." -ForegroundColor Yellow
ollama pull llava:13b

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
