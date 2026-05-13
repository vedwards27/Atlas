$LiveDir = $PSScriptRoot

Write-Host "=== ATLAS RUNTIME ===" -ForegroundColor Cyan
Write-Host "Root: $LiveDir"

# 1. FastAPI server
Write-Host "[1/3] Starting API server on http://localhost:8081 ..." -ForegroundColor Yellow
Start-Process pwsh -ArgumentList "-NoExit", "-Command", `
    "Write-Host 'Atlas API Server' -ForegroundColor Cyan; cd '$LiveDir'; python server.py" `
    -WindowStyle Normal

Start-Sleep -Seconds 2

# 2. Dispatcher
Write-Host "[2/3] Starting dispatcher (Ollama worker loop)..." -ForegroundColor Yellow
Start-Process pwsh -ArgumentList "-NoExit", "-Command", `
    "Write-Host 'Atlas Dispatcher' -ForegroundColor Cyan; cd '$LiveDir'; python dispatcher.py" `
    -WindowStyle Normal

# 3. Dashboard — install deps if needed, then dev server
$DashDir = Join-Path $LiveDir "dashboard"
if (-not (Test-Path (Join-Path $DashDir "node_modules"))) {
    Write-Host "[3/3] Installing dashboard dependencies (first run)..." -ForegroundColor Yellow
    Push-Location $DashDir
    npm install
    Pop-Location
}

Write-Host "[3/3] Starting dashboard on http://localhost:5173 ..." -ForegroundColor Yellow
Start-Process pwsh -ArgumentList "-NoExit", "-Command", `
    "Write-Host 'Atlas Dashboard' -ForegroundColor Cyan; cd '$DashDir'; npm run dev" `
    -WindowStyle Normal

Write-Host ""
Write-Host "All services launched." -ForegroundColor Green
Write-Host "  Dashboard : http://localhost:5173"
Write-Host "  API       : http://localhost:8081"
Write-Host "  API docs  : http://localhost:8081/docs"
