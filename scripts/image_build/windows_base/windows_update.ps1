# Build step "windows_update" of the Windows base (proxmox_base_windows.sh),
# run as SYSTEM through step.ps1: one round of Windows Update through the
# Windows Update Agent API. proxmox_base_windows.sh runs rounds until none is
# left, rebooting when asked.
# Exit 0: nothing left to install. 10: installed, no reboot needed.
# 11: installed, reboot required. 1: an update failed or the API failed.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$session = New-Object -ComObject Microsoft.Update.Session
$session.ClientApplicationID = 'proxmox_base_windows'
$found = $session.CreateUpdateSearcher().Search("IsInstalled=0 and Type='Software' and IsHidden=0").Updates
Write-Output "$($found.Count) update(s) applicable"
if ($found.Count -eq 0) { exit 0 }

$wanted = New-Object -ComObject Microsoft.Update.UpdateColl
foreach ($u in $found) {
    if ($u.InstallationBehavior.CanRequestUserInput) { Write-Output "  skipped (interactive): $($u.Title)"; continue }
    if (-not $u.EulaAccepted) { $u.AcceptEula() }
    Write-Output "  $($u.Title)"
    [void]$wanted.Add($u)
}
if ($wanted.Count -eq 0) { Write-Output 'every applicable update needs user input'; exit 1 }

$downloader = $session.CreateUpdateDownloader()
$downloader.Updates = $wanted
$d = $downloader.Download()
Write-Output "download result $($d.ResultCode) (2 = succeeded)"

$installer = $session.CreateUpdateInstaller()
$installer.Updates = $wanted
$r = $installer.Install()
$failed = 0
for ($i = 0; $i -lt $wanted.Count; $i++) {
    $code = $r.GetUpdateResult($i).ResultCode
    Write-Output ("  result {0}: {1}" -f $code, $wanted.Item($i).Title)
    if ($code -ne 2 -and $code -ne 3) { $failed++ }
}
Write-Output "install result $($r.ResultCode), reboot required: $($r.RebootRequired)"
if ($failed -gt 0) { Write-Output "$failed update(s) failed"; exit 1 }
if ($r.RebootRequired) { exit 11 }
exit 10
