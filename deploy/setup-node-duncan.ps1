# DaveLLM Node Setup — Duncan (RX 5700 XT, 80 GB RAM)
# Role: Large model quality node (CPU inference, 70B)
# Run this script in PowerShell as Administrator
#
# NOTE: The RX 5700 XT (RDNA 1) has limited Ollama GPU support.
# With 80 GB RAM, CPU inference on a 70B Q4 model is reliable.
# Expect ~3-8 tokens/sec — slow but highest quality output.

Write-Host "=== DaveLLM Node Setup: Duncan (80GB RAM) — Quality Node ===" -ForegroundColor Cyan

# 1. Install Ollama if not present
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    Write-Host "Installing Ollama..." -ForegroundColor Yellow
    Invoke-WebRequest -Uri "https://ollama.com/download/OllamaSetup.exe" -OutFile "$env:TEMP\OllamaSetup.exe"
    Start-Process "$env:TEMP\OllamaSetup.exe" -Wait
    Write-Host "Ollama installed. Restart this script after installation completes." -ForegroundColor Green
    exit
}

# 2. Set Ollama to listen on all interfaces + force CPU mode
Write-Host "Configuring Ollama for LAN access (CPU mode)..." -ForegroundColor Yellow
[System.Environment]::SetEnvironmentVariable("OLLAMA_HOST", "0.0.0.0", "User")
[System.Environment]::SetEnvironmentVariable("CUDA_VISIBLE_DEVICES", "", "User")
$env:OLLAMA_HOST = "0.0.0.0"
$env:CUDA_VISIBLE_DEVICES = ""

# 3. Pull the large model (this will take a while — ~40 GB download)
Write-Host "Pulling llama3.1:70b-instruct-q4_K_M (~40 GB)..." -ForegroundColor Yellow
Write-Host "This will take a while on first download. Go get coffee." -ForegroundColor DarkYellow
ollama pull llama3.1:70b

# Also pull a fast fallback
Write-Host "Pulling llama3.2:latest (3B fast fallback)..." -ForegroundColor Yellow
ollama pull llama3.2:latest

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
Write-Host "  {`"id`": `"node-duncan`", `"name`": `"Duncan — Llama 3.1 70B`", `"url`": `"http://${ip}:11434`"}"

# 6. Restart Ollama service
Write-Host ""
Write-Host "Restarting Ollama service..." -ForegroundColor Yellow
Get-Process ollama -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2
Start-Process "ollama" -ArgumentList "serve" -WindowStyle Hidden
Write-Host "Done. Ollama is serving on 0.0.0.0:11434 (CPU mode)" -ForegroundColor Green
