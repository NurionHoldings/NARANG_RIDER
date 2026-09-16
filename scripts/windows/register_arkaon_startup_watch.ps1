param(
    [Parameter(Mandatory = $true)]
    [string]$RepositoryPath
)

$ErrorActionPreference = "Stop"
$scriptPath = Join-Path $RepositoryPath "scripts\windows\run_arkaon_startup_watch.ps1"
if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "ARKAON startup runner was not found in the repository."
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
    '-NoProfile -ExecutionPolicy Bypass -File "{0}" -RepositoryPath "{1}"' -f $scriptPath, $RepositoryPath
)
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "NARANG_RIDER_ARKAON_RESEARCH_WATCH" -Action $action `
    -Trigger $trigger -Settings $settings -Description "Starts privacy-safe ARKAON research review on sign-in." `
    -Force
Write-Output "Registered NARANG_RIDER_ARKAON_RESEARCH_WATCH. No machine identifier is collected."
