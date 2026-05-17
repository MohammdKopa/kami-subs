# Kami Subs — remove the Native Messaging host registration.
$ErrorActionPreference = 'SilentlyContinue'

$HostName = 'com.kamisubs.host'
$Targets = @(
    "HKCU:\Software\Google\Chrome\NativeMessagingHosts\$HostName",
    "HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\$HostName"
)
foreach ($k in $Targets) {
    if (Test-Path $k) {
        Remove-Item -Path $k -Force
        Write-Host "removed: $k"
    }
}
Write-Host 'Done.'
