<#
.SYNOPSIS
  Install pi_sensor_agent.py on a Raspberry Pi and enable it as a systemd service.

.DESCRIPTION
  Copies pi_sensor_agent.py and an /etc/sensor-agent.env file to the Pi, installs
  a systemd unit templated for the target user, and enables it so the agent
  starts on boot and auto-reconnects to the dashboard PC.

  Run this once per Pi. After that, just power on the Pi and it will appear
  in the dashboard automatically.

.PARAMETER User
  SSH username on the Pi (e.g. "pi").

.PARAMETER HostName
  Pi hostname or IP (e.g. "raspberrypi.local" or "192.0.2.42").

.PARAMETER PcHost
  Hostname or IP address of the control PC (dashboard). Written into
  /etc/sensor-agent.env on the Pi as SENSOR_AGENT_PC_HOST.

.PARAMETER PcPort
  TCP port the dashboard listens on. Must match `sensor_dashboard.tcp_port`
  in src/monitoring/config.yaml on the PC. Default: 50001.

.PARAMETER AgentToken
  Optional shared secret, written as SENSOR_AGENT_TOKEN. Must equal
  SENSOR_DASHBOARD_PI_TOKEN on the PC. Omit it if the PC sets no token.

.EXAMPLE
  .\install_pi_agent.ps1 -User pi -HostName raspberrypi.local -PcHost 192.0.2.10
#>
param(
    [Parameter(Mandatory=$true)][string]$User,
    [Parameter(Mandatory=$true)][string]$HostName,
    [Parameter(Mandatory=$true)][string]$PcHost,
    [int]$PcPort = 50001,
    [string]$AgentToken = ""
)

$ErrorActionPreference = "Stop"

# This script lives in src/monitoring/pi/, alongside the files it deploys.
$piDir = $PSScriptRoot
$agentPath = Join-Path $piDir "pi_sensor_agent.py"
$serviceTemplate = Join-Path $piDir "sensor-agent.service"

if (-not (Test-Path $agentPath)) { throw "Agent script not found: $agentPath" }
if (-not (Test-Path $serviceTemplate)) { throw "Service template not found: $serviceTemplate" }

$target = "$User@$HostName"
Write-Host "[*] Installing sensor agent on $target ..." -ForegroundColor Cyan

# 1) Render the service file with the target user substituted for %i.
$serviceText = (Get-Content $serviceTemplate -Raw) -replace "%i", $User
$tmpService = New-TemporaryFile
# 2) Render the env file with the control PC's host/port filled in.
$envText = @"
SENSOR_AGENT_PC_HOST=$PcHost
SENSOR_AGENT_PC_PORT=$PcPort
"@
if ($AgentToken) { $envText += "`nSENSOR_AGENT_TOKEN=$AgentToken" }
$tmpEnv = New-TemporaryFile
try {
    Set-Content -Path $tmpService -Value $serviceText -Encoding utf8 -NoNewline
    Set-Content -Path $tmpEnv -Value $envText -Encoding utf8 -NoNewline

    # 3) Copy agent + service file + env file to the Pi home directory.
    Write-Host "[*] Copying files to ~ on $target"
    & scp $agentPath "${target}:~/pi_sensor_agent.py"
    if ($LASTEXITCODE -ne 0) { throw "scp of agent failed (exit $LASTEXITCODE)" }
    & scp $tmpService "${target}:~/sensor-agent.service"
    if ($LASTEXITCODE -ne 0) { throw "scp of service file failed (exit $LASTEXITCODE)" }
    & scp $tmpEnv "${target}:~/sensor-agent.env"
    if ($LASTEXITCODE -ne 0) { throw "scp of env file failed (exit $LASTEXITCODE)" }
} finally {
    Remove-Item $tmpService -ErrorAction SilentlyContinue
    Remove-Item $tmpEnv -ErrorAction SilentlyContinue
}

# 4) Install dependency, move unit + env files into place, enable & start.
$remoteCmd = @'
set -e
sudo apt-get update -qq
sudo apt-get install -y python3-smbus
sudo mv ~/sensor-agent.service /etc/systemd/system/sensor-agent.service
sudo mv ~/sensor-agent.env /etc/sensor-agent.env
sudo chmod 600 /etc/sensor-agent.env
sudo systemctl daemon-reload
sudo systemctl enable --now sensor-agent.service
sudo systemctl --no-pager --full status sensor-agent.service | head -n 15
'@

Write-Host "[*] Enabling systemd service on $target"
& ssh $target $remoteCmd
if ($LASTEXITCODE -ne 0) { throw "Remote install failed (exit $LASTEXITCODE)" }

Write-Host "[+] Done. The agent will auto-start on boot." -ForegroundColor Green
Write-Host "    Check logs on the Pi with: journalctl -u sensor-agent -f"
