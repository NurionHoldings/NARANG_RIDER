param(
    [Parameter(Mandatory = $true)]
    [string]$RepositoryPath
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $RepositoryPath
$triggerId = "startup-{0}" -f ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
$output = Join-Path $RepositoryPath "build\arkaon-startup-trigger.json"
New-Item -ItemType Directory -Force -Path (Split-Path $output) | Out-Null
$payload = [ordered]@{
    schema_version = "narang.arkaon-startup-trigger.v1"
    trigger_id = $triggerId
    observed_at = [DateTime]::UtcNow.ToString("o")
    source = "WINDOWS_TASK_SCHEDULER"
    machine_identifier_collected = $false
    automatic_learning = $false
    production_change_allowed = $false
}
$payload | ConvertTo-Json | Set-Content -LiteralPath $output -Encoding UTF8
Write-Output "ARKAON research trigger recorded for later evidence review."
