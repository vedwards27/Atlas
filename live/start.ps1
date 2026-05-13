$LiveDir = $PSScriptRoot

Write-Host "=== ATLAS RUNTIME ===" -ForegroundColor Cyan
Write-Host "Root: $LiveDir"

# 1. FastAPI server (port 8082 — avoids conflict with main Atlas Vite on 8081)
Write-Host "[1/4] Starting API server on http://localhost:8082 ..." -ForegroundColor Yellow
Start-Process pwsh -ArgumentList "-NoExit", "-Command", `
    "Write-Host 'Atlas API Server' -ForegroundColor Cyan; cd '$LiveDir'; python -m uvicorn server:app --host 0.0.0.0 --port 8082" `
    -WindowStyle Normal

Start-Sleep -Seconds 3

# 2. Orchestrator (multi-agent supervisor: planner, execution, verifier, recovery, governance, diagnostics)
Write-Host "[2/4] Starting orchestrator (agent supervisor)..." -ForegroundColor Yellow
Start-Process pwsh -ArgumentList "-NoExit", "-Command", `
    "Write-Host 'Atlas Orchestrator' -ForegroundColor Cyan; cd '$LiveDir'; python orchestrator.py" `
    -WindowStyle Normal

# 3. Dispatcher (Ollama worker loop — optional, requires Ollama running)
Write-Host "[3/4] Starting dispatcher (Ollama worker loop)..." -ForegroundColor Yellow
Start-Process pwsh -ArgumentList "-NoExit", "-Command", `
    "Write-Host 'Atlas Dispatcher' -ForegroundColor Cyan; cd '$LiveDir'; python dispatcher.py" `
    -WindowStyle Normal

# 4. Dashboard — build first, then serve via FastAPI (or start dev server)
$DashDir = Join-Path $LiveDir "dashboard"
if (-not (Test-Path (Join-Path $DashDir "node_modules"))) {
    Write-Host "[4/4] Installing dashboard dependencies (first run)..." -ForegroundColor Yellow
    Push-Location $DashDir
    npm install
    Pop-Location
}

# Build the dashboard so FastAPI can serve it at http://localhost:8082
Write-Host "[4/4] Building dashboard (served by API at http://localhost:8082) ..." -ForegroundColor Yellow
Push-Location $DashDir
npm run build
Pop-Location

Write-Host ""
Write-Host "All services launched." -ForegroundColor Green
Write-Host "  Dashboard : http://localhost:8082"
Write-Host "  API       : http://localhost:8082/api"
Write-Host "  API docs  : http://localhost:8082/docs"
Write-Host ""
Write-Host "Dev mode (hot-reload): cd dashboard && npm run dev" -ForegroundColor DarkGray
Write-Host "  Dev dashboard: http://localhost:5173 (proxies API to :8082)" -ForegroundColor DarkGray
