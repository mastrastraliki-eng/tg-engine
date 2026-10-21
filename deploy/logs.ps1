# Saves the last 3 hours of the engine journal (without the alert channel) to a file next to the .bat.
param([Parameter(Mandatory = $true)][string]$Log)

$Key = 'C:\Users\bodzin7\.ssh\clipfactory'
$HostName = 'ubuntu@144.91.123.244'
$cmd = "sudo journalctl -u tg-engine --since '-3h' --no-pager -o short-iso | grep -v '\[alerts\] published' | tail -n 300"
ssh -i $Key -o ConnectTimeout=20 $HostName $cmd 2>&1 | Out-File -FilePath $Log -Encoding UTF8
Write-Host "Saved: $Log"
