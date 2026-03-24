# DaveLLM Node Setup — MSI GP66 Leopard (RTX 3070)
# Role: Fast general inference (Llama 3.2 7B)
# Run this script in PowerShell as Administrator

Write-Host "=== DaveLLM Node Setup: GP66 (RTX 3070) ===" -ForegroundColor Cyan

# 1. Install Ollama if not present
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    Write-Host "Installing Ollama..." -ForegroundColor Yellow
    Invoke-WebRequest -Uri "https://ollama.com/download/OllamaSetup.exe" -OutFile "$env:TEMP\OllamaSetup.exe"
    Start-Process "$env:TEMP\OllamaSetup.exe" -Wait
    Write-Host "Ollama installed. Restart this script after installation completes." -ForegroundColor Green
    exit
}

# 2. Set Ollama to listen on all interfaces (required for LAN access)
Write-Host "Configuring Ollama for LAN access..." -ForegroundColor Yellow
[System.Environment]::SetEnvironmentVariable("OLLAMA_HOST", "0.0.0.0", "User")
$env:OLLAMA_HOST = "0.0.0.0"

# 3. Pull the model
Write-Host "Pulling llama3.2:latest (3B fast)..." -ForegroundColor Yellow
ollama pull llama3.2:latest

Write-Host "Pulling llama3.1:8b (general purpose)..." -ForegroundColor Yellow
ollama pull llama3.1:8b

# 4. Open firewall port
Write-Host "Opening firewall port 11434..." -ForegroundColor Yellow
New-NetFirewallRule -DisplayName "Ollama LAN" -Direction Inbound -Protocol TCP -LocalPort 11434 -Action Allow -ErrorAction SilentlyContinue

# 5. Verify
Write-Host ""
Write-Host "=== Verification ===" -ForegroundColor Cyan
$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.*" } | Select-Object -First 1).IPAddress
Write-Host "This machine's LAN IP: $ip" -ForegroundColor Green
Write-Host "Ollama endpoint: http://${ip}:11434" -ForegroundColor Green
Write-Host ""
Write-Host "Test from Mac:" -ForegroundColor Yellow
Write-Host "  curl http://${ip}:11434/api/tags"
Write-Host ""
Write-Host "Add to DaveLLM router config:" -ForegroundColor Yellow
Write-Host "  {`"id`": `"node-gp66`", `"name`": `"GP66 — Llama 3.1 8B`", `"url`": `"http://${ip}:11434`"}"

# 6. Restart Ollama service to pick up OLLAMA_HOST
Write-Host ""
Write-Host "Restarting Ollama service..." -ForegroundColor Yellow
Get-Process ollama -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2
Start-Process "ollama" -ArgumentList "serve" -WindowStyle Hidden
Write-Host "Done. Ollama is serving on 0.0.0.0:11434" -ForegroundColor Green
