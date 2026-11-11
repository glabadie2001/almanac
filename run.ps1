#!/usr/bin/env pwsh
<#
.SYNOPSIS
  Almanac launcher for PowerShell (`pwsh` / Windows PowerShell).

.EXAMPLE
  .\run.ps1                       # Start Almanac + Mesh Hub and open the browser (or open tab if already running)
  .\run.ps1 url                   # Print the signed-in dashboard URL
  .\run.ps1 mesh roster           # Run Mesh CLI (roster | context | contacts | send | wait)
  .\run.ps1 test                  # Run the unit test suite
  .\run.ps1 --port 8820 --open    # Pass any flags directly to `python -m almanac`
#>
$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $PSScriptRoot

function Resolve-PythonCommand {
    if ($env:PYTHON) {
        try {
            & $env:PYTHON -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" *> $null
            if ($LASTEXITCODE -eq 0) { return @($env:PYTHON) }
        } catch {}
    }
    foreach ($cand in @("python", "python3")) {
        try {
            & $cand -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" *> $null
            if ($LASTEXITCODE -eq 0) { return @($cand) }
        } catch {}
    }
    try {
        & py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" *> $null
        if ($LASTEXITCODE -eq 0) { return @("py", "-3") }
    } catch {}
    throw "Python 3.11+ not found on PATH (or set `$env:PYTHON to your python.exe path)."
}

$py = @(Resolve-PythonCommand)
$pyExe = $py[0]
$pyPrefix = if ($py.Count -gt 1) { $py[1..($py.Count - 1)] } else { @() }

$first = if ($args.Count -gt 0) { [string]$args[0] } else { "" }
$rest  = if ($args.Count -gt 1) { $args[1..($args.Count - 1)] } else { @() }

# Runs Almanac and starts it again while it exits with 75 (a restart it was asked for: POST /api/mesh/restart).
# ALMANAC_LAUNCHER tells the door that something will bring it back; the browser opens on the first start only.
function Invoke-Almanac([bool]$Open, [object[]]$Flags) {
    $env:ALMANAC_LAUNCHER = "1"
    while ($true) {
        $openFlag = @(if ($Open) { "--open" })
        & $pyExe @pyPrefix -m almanac @openFlag @Flags | Out-Host
        if ($LASTEXITCODE -ne 75) { return $LASTEXITCODE }
        $Open = $false
        Write-Host "Almanac asked to restart; starting it again"
    }
}

switch ($first) {
    "" {
        $probe = "import json, urllib.request; from almanac.config import load; p = load('config.json')['port']; r = urllib.request.urlopen(f'http://127.0.0.1:{p}/ident', timeout=1.0); d = json.loads(r.read().decode()); raise SystemExit(0 if d.get('kind') == 'almanac' else 1)"
        $alreadyUp = $false
        try {
            & $pyExe @pyPrefix -c $probe *> $null
            if ($LASTEXITCODE -eq 0) { $alreadyUp = $true }
        } catch {}
        if ($alreadyUp) {
            $loginUrl = (& $pyExe @pyPrefix -m almanac --url).Trim()
            Write-Host "Almanac is already running -> $loginUrl"
            Start-Process $loginUrl | Out-Null
            exit 0
        }
        exit (Invoke-Almanac $true @())
    }
    "restart" {
        $stopScript = "import json, os, signal, time, urllib.request; from almanac.config import load; p = load('config.json')['port']; r = urllib.request.urlopen(f'http://127.0.0.1:{p}/ident', timeout=1.0); d = json.loads(r.read().decode()); pid = int(d.get('pid') or 0); os.kill(pid, signal.SIGTERM) if d.get('kind') == 'almanac' and pid > 0 else None; time.sleep(0.4)"
        try { & $pyExe @pyPrefix -c $stopScript *> $null } catch {}
        exit (Invoke-Almanac $true $rest)
    }
    "url" {
        & $pyExe @pyPrefix -m almanac --url @rest
        exit $LASTEXITCODE
    }
    "ingest" {
        & $pyExe @pyPrefix -m almanac --ingest @rest
        exit $LASTEXITCODE
    }
    "mesh" {
        & $pyExe @pyPrefix -m almanac.mcp_mesh --cli @rest
        exit $LASTEXITCODE
    }
    { $_ -in @("test", "tests") } {
        & $pyExe @pyPrefix -m unittest discover tests @rest
        exit $LASTEXITCODE
    }
    default {
        exit (Invoke-Almanac $false $args)
    }
}

