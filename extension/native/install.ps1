# Kami Subs — install the Native Messaging host for Chrome (current user).
#
# What this does:
#   1) Renders the host manifest template with the absolute path to launcher.bat
#   2) Writes it next to the template as com.kamisubs.host.json
#   3) Registers it in HKCU so Chrome can find it:
#        HKCU\Software\Google\Chrome\NativeMessagingHosts\com.kamisubs.host
#        (default) -> full path to com.kamisubs.host.json
#
# Run from PowerShell (no admin needed — HKCU only):
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#
# After install:
#   - Reload the extension in chrome://extensions (so it picks up the new
#     manifest 'key' + 'nativeMessaging' permission).
#   - Confirm the extension ID matches the one baked into the host JSON
#     (cbahglicegngghebkgnegbbgbpfdncka). If you ever change manifest 'key',
#     re-run this script.

$ErrorActionPreference = 'Stop'

$HostDir       = Split-Path -Parent $MyInvocation.MyCommand.Path
$Template      = Join-Path $HostDir 'com.kamisubs.host.json.template'
$Manifest      = Join-Path $HostDir 'com.kamisubs.host.json'
$LauncherBat   = Join-Path $HostDir 'launcher.bat'
$HostName      = 'com.kamisubs.host'

if (-not (Test-Path $Template))    { throw "template missing: $Template" }
if (-not (Test-Path $LauncherBat)) { throw "launcher.bat missing: $LauncherBat" }

# Render template — JSON requires forward slashes or escaped backslashes.
$bat = $LauncherBat -replace '\\', '\\'
(Get-Content $Template -Raw) -replace '__LAUNCHER_BAT_PATH__', $bat |
    Set-Content -Path $Manifest -Encoding UTF8 -NoNewline

Write-Host "wrote $Manifest"

# Register for Chrome (and Edge — both honor the Chrome key).
$Targets = @(
    'HKCU:\Software\Google\Chrome\NativeMessagingHosts',
    'HKCU:\Software\Microsoft\Edge\NativeMessagingHosts'
)
foreach ($base in $Targets) {
    if (-not (Test-Path $base)) { New-Item -Path $base -Force | Out-Null }
    $key = Join-Path $base $HostName
    if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
    Set-ItemProperty -Path $key -Name '(default)' -Value $Manifest
    Write-Host "registered: $key  ->  $Manifest"
}

Write-Host ''
Write-Host 'Done. Reload the extension at chrome://extensions and click Start.'
