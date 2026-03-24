# DaveLLM Node Setup — MSI Katana #1 (RTX 4060)
# Role: Code generation model
# Run this script in PowerShell as Administrator

Write-Host "=== DaveLLM Node Setup: Katana 1 (RTX 4060) — Code ===" -ForegroundColor Cyan

# 1. Install Ollama if not present
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    Write-Host "Installing Ollama..." -ForegroundColor Yellow
    Invoke-WebRequest -Uri "https://ollama.com/download/OllamaSetup.exe" -OutFile "$env:TEMP\OllamaSetup.exe"
    Start-Process "$env:TEMP\OllamaSetup.exe" -Wait
    Write-Host "Ollama installed. Restart this script after installation completes." -ForegroundColor Green
    exit
}

# 2. Set Ollama to listen on all interfaces
Write-Host "Configuring Ollama for LAN access..." -ForegroundColor Yellow
[System.Environment]::SetEnvironmentVariable("OLLAMA_HOST", "0.0.0.0", "User")
$env:OLLAMA_HOST = "0.0.0.0"

# 3. Pull code-focused models
Write-Host "Pulling deepseek-coder-v2:16b (code generation)..." -ForegroundColor Yellow
ollama pull deepseek-coder-v2:16b

Write-Host "Pulling codellama:7b (fast code fallback)..." -ForegroundColor Yellow
ollama pull codellama:7b

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
Write-Host "  {`"id`": `"node-katana-1`", `"name`": `"Katana 1 — DeepSeek Coder V2 16B`", `"url`": `"http://${ip}:11434`"}"

# 6. Restart Ollama service
Write-Host ""
Write-Host "Restarting Ollama service..." -ForegroundColor Yellow
Get-Process ollama -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2
Start-Process "ollama" -ArgumentList "serve" -WindowStyle Hidden
Write-Host "Done. Ollama is serving on 0.0.0.0:11434" -ForegroundColor Green
