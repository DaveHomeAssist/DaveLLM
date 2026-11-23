$ErrorActionPreference = 'Stop'

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$comfyRoot = Join-Path ([Environment]::GetFolderPath('Desktop')) 'ComfyUI_windows_portable'
$comfyPython = Join-Path $comfyRoot 'python_embeded\python.exe'
$comfyMain = Join-Path $comfyRoot 'ComfyUI\main.py'
$davePython = Join-Path $repo '.venv\Scripts\python.exe'
$dataDir = Join-Path $env:LOCALAPPDATA 'DaveLLM\data'
$comfyUrl = 'http://127.0.0.1:8188'

if (-not (Test-Path -LiteralPath $davePython)) {
    throw "DaveLLM virtual environment is missing. Run: python -m venv .venv"
}

try {
    Invoke-RestMethod -Uri "$comfyUrl/system_stats" -TimeoutSec 2 | Out-Null
} catch {
    if (-not (Test-Path -LiteralPath $comfyPython) -or -not (Test-Path -LiteralPath $comfyMain)) {
        throw "ComfyUI portable is missing from $comfyRoot"
    }
    Start-Process -FilePath $comfyPython `
        -ArgumentList @('-s', 'ComfyUI\main.py', '--windows-standalone-build', '--listen', '127.0.0.1', '--port', '8188', '--lowvram', '--disable-auto-launch') `
        -WorkingDirectory $comfyRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $comfyRoot 'davellm-comfy.stdout.log') `
        -RedirectStandardError (Join-Path $comfyRoot 'davellm-comfy.stderr.log') | Out-Null

    $ready = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        Start-Sleep -Seconds 1
        try {
            Invoke-RestMethod -Uri "$comfyUrl/system_stats" -TimeoutSec 2 | Out-Null
            $ready = $true
            break
        } catch {}
    }
    if (-not $ready) { throw 'ComfyUI did not become ready within 60 seconds' }
}

New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
$secretBytes = [byte[]]::new(32)
[Security.Cryptography.RandomNumberGenerator]::Fill($secretBytes)
$env:DAVE_API_KEY = [Convert]::ToHexString($secretBytes).ToLowerInvariant()
$env:DAVE_DATA_DIR = $dataDir
$env:DAVE_COMFY_URL = $comfyUrl
$env:DAVE_COMFY_CHECKPOINT = 'sd_xl_turbo_1.0_fp16.safetensors'
# Keep Enhance prompt on a model small enough to share the 8 GB GPU with ComfyUI.
if (-not $env:DAVE_IMAGE_ENHANCE_MODEL) { $env:DAVE_IMAGE_ENHANCE_MODEL = 'llama3:latest' }
$env:DAVE_PYTHON = $davePython
$env:PYTHONIOENCODING = 'utf-8'

Set-Location $repo
npm start
