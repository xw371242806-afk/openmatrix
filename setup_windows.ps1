param([string]$DataDir = "", [switch]$NoBrowser)
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Find-Python312 {
    foreach ($candidate in @("py -3.12", "python", "python3")) {
        try {
            $parts = $candidate.Split(" ")
            $version = if ($parts.Count -eq 2) { & $parts[0] $parts[1] -V 2>&1 } else { & $candidate -V 2>&1 }
            if ($LASTEXITCODE -eq 0 -and "$version" -match "Python 3\.12\.") { return $candidate }
        } catch {}
    }
    return $null
}

$python = Find-Python312
if (-not $python) {
    Write-Host "Python 3.12 is required. Install it with: winget install Python.Python.3.12" -ForegroundColor Yellow
    exit 1
}
$venvPython = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    $parts = $python.Split(" ")
    if ($parts.Count -eq 2) { & $parts[0] $parts[1] -m venv venv } else { & $python -m venv venv }
    if ($LASTEXITCODE -ne 0) { throw "Python 3.12 could not create the virtual environment" }
}
if (-not (Test-Path -LiteralPath $venvPython)) { throw "Could not create venv with Python 3.12" }
$venvVersion = (& $venvPython -V 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $venvVersion -notmatch "Python 3\.12\.") { throw "Existing venv is not Python 3.12: $venvVersion" }

$reqFiles = @((Join-Path $PSScriptRoot "requirements.txt"), (Join-Path $PSScriptRoot "requirements.lock"))
$missing = @($reqFiles | Where-Object { -not (Test-Path -LiteralPath $_) })
if ($missing.Count -gt 0) { throw "Required dependency lock inputs are missing: $($missing -join ', ')" }
$reqHash = ($reqFiles | ForEach-Object { if (Test-Path -LiteralPath $_) { (Get-FileHash -Algorithm SHA256 -LiteralPath $_).Hash } }) -join ":"
$hashFile = Join-Path $PSScriptRoot "venv\.req_hash"
$oldHash = if (Test-Path -LiteralPath $hashFile) { (Get-Content -Raw -LiteralPath $hashFile).Trim() } else { "" }
if ($oldHash -ne $reqHash) {
    & $venvPython -m pip install -r requirements.txt -c requirements.lock
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed; cached hash was not updated" }
    Set-Content -LiteralPath $hashFile -Value $reqHash -NoNewline
}

$distIndex = Join-Path $PSScriptRoot "ui\dist\index.html"
if (-not (Test-Path -LiteralPath $distIndex)) {
    $node = Get-Command node -ErrorAction SilentlyContinue
    if (-not $node) { throw "ui/dist/index.html is absent and Node.js >= 24 is not installed" }
    $nodeVersion = (& node -v).Trim()
    try { $nodeMajor = ([version]$nodeVersion.TrimStart('v')).Major } catch { throw "Could not parse Node.js version: $nodeVersion" }
    if ($nodeMajor -lt 24) { throw "Node.js >= 24 is required (found $nodeVersion)" }
    Push-Location (Join-Path $PSScriptRoot "ui")
    try { & npm ci; if ($LASTEXITCODE -ne 0) { throw "npm ci failed" }; & npm run build; if ($LASTEXITCODE -ne 0) { throw "npm run build failed" } }
    finally { Pop-Location }
}

if ([string]::IsNullOrWhiteSpace($DataDir)) { $DataDir = Join-Path $PSScriptRoot "data" }
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$env:OPENMATRIX_DATA_DIR = (Resolve-Path -LiteralPath $DataDir).Path
if (-not $env:OPENMATRIX_PORT) { $env:OPENMATRIX_PORT = "8000" }
if (-not $env:OPENMATRIX_HOST) { $env:OPENMATRIX_HOST = "127.0.0.1" }

function Test-BackendHealth([string]$Uri) {
    # The health endpoint is local; never send it through a system proxy.
    $request = [System.Net.WebRequest]::Create($Uri)
    $request.Proxy = $null
    $request.Timeout = 2000
    $response = $request.GetResponse()
    try {
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
        try { $payload = $reader.ReadToEnd() | ConvertFrom-Json }
        finally { $reader.Dispose() }
        return ([int]$response.StatusCode -eq 200 -and $payload.service -eq "openmatrix")
    } finally { $response.Close() }
}

$healthHost = if ($env:OPENMATRIX_HOST -in @("::", "::1")) { "[::1]" } else { "127.0.0.1" }
$health = "http://${healthHost}:$($env:OPENMATRIX_PORT)/api/v1/health"
try {
    if (Test-BackendHealth $health) {
        Write-Host "OpenMatrix is already healthy at $health; reusing the existing process."
        if (-not $NoBrowser) { Start-Process "http://localhost:$($env:OPENMATRIX_PORT)/ui/" }
        exit 0
    }
} catch {}

$stdoutLog = Join-Path $env:OPENMATRIX_DATA_DIR "startup.stdout.log"
$stderrLog = Join-Path $env:OPENMATRIX_DATA_DIR "startup.stderr.log"
$server = Start-Process -FilePath $venvPython -ArgumentList "main.py" -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -PassThru
try {
    $ready = $false
    $clock = [Diagnostics.Stopwatch]::StartNew()
    while ($clock.Elapsed.TotalSeconds -lt 120) {
        if ($server.HasExited) { throw "Backend exited before health became ready (exit code $($server.ExitCode))" }
        try { if (Test-BackendHealth $health) { $ready = $true; break } } catch {}
        Start-Sleep -Milliseconds 250
    }
    if (-not $ready) { throw "Backend did not become healthy within $([math]::Round($clock.Elapsed.TotalSeconds, 1)) seconds (PID $($server.Id)); see $stderrLog" }
    Write-Host "OpenMatrix backend is healthy (PID $($server.Id), startup $([math]::Round($clock.Elapsed.TotalSeconds, 1))s)."
    if (-not $NoBrowser) { Start-Process "http://localhost:$($env:OPENMATRIX_PORT)/ui/" }
} catch {
    if ($server -and -not $server.HasExited) { Stop-Process -Id $server.Id -ErrorAction SilentlyContinue }
    throw
}
