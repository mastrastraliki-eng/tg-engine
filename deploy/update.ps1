# Sends code and channel configs from this PC to the VPS and restarts the engine.
# .env and the database on the server stay untouched. Started by the .bat next to it.
param([Parameter(Mandatory = $true)][string]$Log)

$ErrorActionPreference = 'Continue'
$Key = 'C:\Users\bodzin7\.ssh\clipfactory'
$HostName = 'ubuntu@144.91.123.244'
$Src = 'C:\Users\bodzin7\Downloads'
$Tgz = Join-Path $env:TEMP 'tg-engine-update.tgz'
$Remote = Join-Path $env:TEMP 'tg-update.sh'

Set-Content -Path $Log -Value "tg-engine update $(Get-Date -Format s)" -Encoding UTF8

function Write-Log([string]$Text) {
    Write-Host $Text
    Add-Content -Path $Log -Value $Text -Encoding UTF8
}

function Invoke-Logged([string]$Title, [scriptblock]$Cmd) {
    Write-Log ""
    Write-Log "==== $Title"
    & $Cmd 2>&1 | ForEach-Object { Write-Log ([string]$_) }
    $code = $LASTEXITCODE
    Write-Log "==== exit code: $code"
    return $code
}

Remove-Item $Tgz -Force -ErrorAction SilentlyContinue
$null = Invoke-Logged 'Building archive' {
    tar.exe -czf $Tgz -C $Src --exclude=.env --exclude=data --exclude=.venv --exclude=.pytest_cache --exclude=__pycache__ --exclude='backup*' tg-engine
}
if (-not (Test-Path $Tgz)) { Write-Log 'ERROR: archive was not created'; exit 1 }

$script = @(
    'set -u'
    'rm -rf /root/tg-engine-src /tmp/tg-engine'
    'tar -xzf /tmp/tg-engine-update.tgz -C /tmp || { echo "ERROR: tar failed"; exit 1; }'
    'mv /tmp/tg-engine /root/tg-engine-src'
    'cd /root/tg-engine-src'
    "sed -i 's/\r`$//' deploy/install.sh deploy/tg-engine.service"
    'bash deploy/install.sh; echo "install.sh exit: $?"'
    'echo; echo "==== waiting 30s"; sleep 30'
    'echo "service: $(systemctl is-active tg-engine)"'
    'journalctl -u tg-engine -n 25 --no-pager'
    'echo; echo "==== report"; cd /opt/tg-engine && sudo -u tgengine .venv/bin/python -m engine health --send; cd /'
    'rm -rf /root/tg-engine-src /tmp/tg-engine /tmp/tg-engine-update.tgz /tmp/tg-update.sh'
    'echo "cleanup done"'
) -join "`n"
[IO.File]::WriteAllText($Remote, $script + "`n", (New-Object Text.UTF8Encoding $false))

$code = Invoke-Logged 'Upload (scp)' { scp -i $Key -o ConnectTimeout=20 $Tgz $Remote "${HostName}:/tmp/" }
if ($code -ne 0) {
    Write-Log 'ERROR: upload failed, nothing changed on the server'
} else {
    $null = Invoke-Logged 'Update on server (ssh)' { ssh -t -i $Key -o ConnectTimeout=20 $HostName 'sudo bash /tmp/tg-update.sh' }
}

Remove-Item $Tgz, $Remote -Force -ErrorAction SilentlyContinue
Write-Log ""
Write-Log "Done $(Get-Date -Format s). Log: $Log"
