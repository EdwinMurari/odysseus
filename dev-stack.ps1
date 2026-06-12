#Requires -Version 5.1
<#
.SYNOPSIS
  Starts and manages the full Odysseus Docker development stack.

.EXAMPLE
  .\dev-stack.ps1 rebuild
  .\dev-stack.ps1 rebuild -Scope All
  .\dev-stack.ps1 restart
  .\dev-stack.ps1 logs
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet("up", "rebuild", "restart", "status", "logs", "down")]
    [string]$Action = "up",

    [ValidateSet("Auto", "Nvidia", "Amd", "None")]
    [string]$Gpu = "Auto",

    [ValidateSet("Auto", "On", "Off")]
    [string]$Capabilities = "Auto",

    # Memory safety-net ceilings (docker/limits.yml). Auto = On when the
    # overlay file exists. Use -Limits Off only when debugging OOM kills.
    [ValidateSet("Auto", "On", "Off")]
    [string]$Limits = "Auto",

    [ValidateSet("App", "All")]
    [string]$Scope = "App",

    [int]$ReadyTimeoutSeconds = 180
)

$ErrorActionPreference = "Stop"
$RepoRoot = $PSScriptRoot
Set-Location -LiteralPath $RepoRoot

if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot ".env"))) {
    Copy-Item -LiteralPath (Join-Path $RepoRoot ".env.example") `
        -Destination (Join-Path $RepoRoot ".env")
    Write-Host "Created .env from .env.example." -ForegroundColor Yellow
}

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments
    )

    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Command failed with exit code $LASTEXITCODE"
    }
}

function Get-DotEnvValue([string]$Name, [string]$Default) {
    $envPath = Join-Path $RepoRoot ".env"
    if (-not (Test-Path -LiteralPath $envPath)) {
        return $Default
    }

    $match = Get-Content -LiteralPath $envPath |
        Where-Object { $_ -match "^\s*$([regex]::Escape($Name))=(.*)$" } |
        Select-Object -Last 1
    if (-not $match) {
        return $Default
    }

    return (($match -split "=", 2)[1]).Trim().Trim('"').Trim("'")
}

function Test-DockerEngine {
    $previousErrorAction = $ErrorActionPreference
    try {
        # An offline Docker engine is an expected probe result. Do not let its
        # stderr become a terminating NativeCommandError under the script-wide
        # ErrorActionPreference = Stop.
        $ErrorActionPreference = "SilentlyContinue"
        & docker info *> $null
        return $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }
}

function Start-DockerEngine {
    if (Test-DockerEngine) {
        return
    }

    if ($env:OS -ne "Windows_NT") {
        throw "Docker is installed but its engine is not running."
    }

    $desktopCandidates = @(
        @(
            (Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"),
            (Join-Path $env:LOCALAPPDATA "Docker\Docker Desktop.exe")
        ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
    )

    if (-not $desktopCandidates) {
        throw "Docker Desktop is not running and Docker Desktop.exe was not found."
    }

    Write-Step "Starting Docker Desktop"
    Start-Process -FilePath $desktopCandidates[0] -WindowStyle Hidden

    $deadline = (Get-Date).AddSeconds(120)
    do {
        Start-Sleep -Seconds 3
        if (Test-DockerEngine) {
            return
        }
    } while ((Get-Date) -lt $deadline)

    throw "Docker Desktop did not become ready within 120 seconds."
}

function Resolve-GpuMode {
    if ($Gpu -ne "Auto") {
        return $Gpu
    }
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
        return "Nvidia"
    }
    return "None"
}

function Resolve-CapabilitiesMode {
    if ($Capabilities -ne "Auto") {
        return $Capabilities
    }

    $parent = Split-Path -Parent $RepoRoot
    $ready = (Test-Path -LiteralPath (Join-Path $RepoRoot "data\capabilities.yaml")) -and
        (Test-Path -LiteralPath (Join-Path $parent "pain-miner")) -and
        (Test-Path -LiteralPath (Join-Path $parent "stock-research"))
    if ($ready) {
        return "On"
    }
    return "Off"
}

function Resolve-LimitsMode {
    if ($Limits -ne "Auto") {
        return $Limits
    }
    if (Test-Path -LiteralPath (Join-Path $RepoRoot "docker\limits.yml")) {
        return "On"
    }
    return "Off"
}

function Show-WslConfigHint {
    if ($env:OS -ne "Windows_NT") {
        return
    }
    $wslConfig = Join-Path $env:USERPROFILE ".wslconfig"
    if (-not (Test-Path -LiteralPath $wslConfig)) {
        Write-Host "Hint: $wslConfig not found - WSL2 may balloon to ~16GB RAM and hold it." -ForegroundColor Yellow
        Write-Host "      Copy docker\wslconfig.example there, then run 'wsl --shutdown' once." -ForegroundColor Yellow
    }
}

function Get-ComposeArguments([string]$GpuMode, [string]$CapabilitiesMode, [string]$LimitsMode) {
    $arguments = @(
        "--project-directory", $RepoRoot,
        "--env-file", (Join-Path $RepoRoot ".env"),
        "-f", (Join-Path $RepoRoot "docker-compose.yml")
    )

    if ($GpuMode -eq "Nvidia") {
        $arguments += @("-f", (Join-Path $RepoRoot "docker\gpu.nvidia.yml"))
    } elseif ($GpuMode -eq "Amd") {
        $arguments += @("-f", (Join-Path $RepoRoot "docker\gpu.amd.yml"))
    }

    if ($CapabilitiesMode -eq "On") {
        $parent = Split-Path -Parent $RepoRoot
        foreach ($requiredPath in @(
            (Join-Path $RepoRoot "data\capabilities.yaml"),
            (Join-Path $parent "pain-miner"),
            (Join-Path $parent "stock-research")
        )) {
            if (-not (Test-Path -LiteralPath $requiredPath)) {
                throw "Capabilities requested, but required path is missing: $requiredPath"
            }
        }
        $arguments += @(
            "-f",
            (Join-Path $RepoRoot "docker-compose.capabilities.example.yml")
        )
    }

    if ($LimitsMode -eq "On") {
        $arguments += @("-f", (Join-Path $RepoRoot "docker\limits.yml"))
        if ($CapabilitiesMode -eq "On") {
            # Worker limits live in a second file because these services only
            # exist when the capabilities overlay is merged in.
            $arguments += @("-f", (Join-Path $RepoRoot "docker\limits.capabilities.yml"))
        }
    }

    return $arguments
}

function Wait-OdysseusReady {
    $port = Get-DotEnvValue "APP_PORT" "7000"
    $uri = "http://127.0.0.1:$port/api/ready"
    $deadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)

    Write-Step "Waiting for Odysseus readiness at $uri"
    do {
        try {
            $response = Invoke-RestMethod -Uri $uri -TimeoutSec 5
            if ($response.ready -eq $true) {
                Write-Host "Odysseus is ready: http://127.0.0.1:$port" -ForegroundColor Green
                return
            }
        } catch {
            # Startup connection failures are expected while containers settle.
        }
        Start-Sleep -Seconds 3
    } while ((Get-Date) -lt $deadline)

    & docker compose @script:ComposeArgs logs --tail=120 odysseus
    throw "Odysseus did not become ready within $ReadyTimeoutSeconds seconds."
}

function Confirm-Gpu([string]$GpuMode) {
    if ($GpuMode -eq "Nvidia") {
        Write-Step "Verifying NVIDIA GPU passthrough"
        Invoke-Checked docker compose @script:ComposeArgs exec -T odysseus nvidia-smi -L
    } elseif ($GpuMode -eq "Amd") {
        Write-Step "Verifying AMD GPU passthrough"
        Invoke-Checked docker compose @script:ComposeArgs exec -T odysseus sh -lc "test -e /dev/kfd && test -d /dev/dri"
    }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker was not found on PATH. Install Docker Desktop first."
}

Start-DockerEngine
Show-WslConfigHint
Invoke-Checked docker compose version

$GpuMode = Resolve-GpuMode
$CapabilitiesMode = Resolve-CapabilitiesMode
$LimitsMode = Resolve-LimitsMode
$script:ComposeArgs = Get-ComposeArguments $GpuMode $CapabilitiesMode $LimitsMode

Write-Host "GPU: $GpuMode | Capabilities: $CapabilitiesMode | Limits: $LimitsMode | Rebuild scope: $Scope"
Write-Step "Validating effective Compose configuration"
Invoke-Checked docker compose @script:ComposeArgs config --quiet

switch ($Action) {
    "up" {
        Write-Step "Starting the existing stack"
        Invoke-Checked docker compose @script:ComposeArgs up -d --remove-orphans
        Wait-OdysseusReady
        Confirm-Gpu $GpuMode
    }
    "rebuild" {
        Write-Step "Rebuilding Odysseus"
        if ($Scope -eq "All") {
            Invoke-Checked docker compose @script:ComposeArgs up -d --build --force-recreate --remove-orphans
        } else {
            # Start dependencies first, then rebuild only the application image.
            Invoke-Checked docker compose @script:ComposeArgs up -d --remove-orphans
            Invoke-Checked docker compose @script:ComposeArgs build odysseus
            Invoke-Checked docker compose @script:ComposeArgs up -d --force-recreate --no-deps odysseus
        }
        Wait-OdysseusReady
        Confirm-Gpu $GpuMode
    }
    "restart" {
        Write-Step "Restarting containers without rebuilding images"
        Invoke-Checked docker compose @script:ComposeArgs restart
        Wait-OdysseusReady
        Confirm-Gpu $GpuMode
    }
    "status" {
        Invoke-Checked docker compose @script:ComposeArgs ps
    }
    "logs" {
        & docker compose @script:ComposeArgs logs --tail=200 -f
    }
    "down" {
        Write-Step "Stopping containers while preserving all volumes and caches"
        Invoke-Checked docker compose @script:ComposeArgs down
    }
}
