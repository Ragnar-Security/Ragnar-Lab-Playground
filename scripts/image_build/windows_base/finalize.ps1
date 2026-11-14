# Build step "finalize" of the Windows base (proxmox_base_windows.sh), run as
# SYSTEM through step.ps1, just before sysprep. Leaves the base with:
#   - no SSH host keys, no authorized keys; sshd disabled until a clone's
#     first-boot setup has finished (SetupComplete.cmd then enables and starts
#     it: before that, the clone still reboots once);
#   - the sysprep answer file (no password in it) and randomize-admin.ps1,
#     which gives each clone's Administrator its own random password;
#   - no automatic logon, empty event logs.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
function Step($m) { Write-Output "== $m" }
$work = 'C:\basebuild'
$ssh = 'C:\ProgramData\ssh'

Step 'no reboot pending (sysprep refuses one)'
$pending = @(
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending',
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired'
) | Where-Object { Test-Path $_ }
if ($pending) { throw "a reboot is pending: $($pending -join ', ')" }

Step 'apps installed for a user but not provisioned (sysprep refuses them, 0x80073CF2)'
# The cumulative update installs Microsoft Edge (Microsoft.MicrosoftEdge.Stable)
# for Administrator only; sysprep /generalize then fails. Remove every such
# package that Windows allows to be removed.
$provisioned = @(Get-AppxProvisionedPackage -Online | ForEach-Object { $_.DisplayName })
$stray = @(Get-AppxPackage -AllUsers | Where-Object {
    -not $_.IsFramework -and -not $_.NonRemovable -and $provisioned -notcontains $_.Name -and
    ($_.PackageUserInformation | Where-Object { $_.InstallState -eq 'Installed' })
})
foreach ($p in $stray) {
    Write-Output "removing $($p.PackageFullName)"
    Remove-AppxPackage -Package $p.PackageFullName -AllUsers
}
Write-Output "$($stray.Count) package(s) removed"

Step 'licence: sysprep /generalize needs a rearm left'
$dlv = (& cscript.exe //nologo C:\Windows\System32\slmgr.vbs /dlv) -join "`n"
Write-Output $dlv
if ($dlv -match 'Remaining Windows rearm count:\s*(\d+)' -and [int]$Matches[1] -lt 1) {
    throw 'no Windows rearm left; sysprep /generalize would fail'
}

Step 'sshd: disabled until first-boot setup finishes; no host or authorized keys'
Stop-Service -Name sshd
Set-Service -Name sshd -StartupType Disabled
Remove-Item -Force -ErrorAction SilentlyContinue "$ssh\ssh_host_*", "$ssh\administrators_authorized_keys"
Get-ChildItem -Path 'C:\Users\*\.ssh\authorized_keys' -Force -ErrorAction SilentlyContinue | Remove-Item -Force
$left = @(Get-ChildItem -Path "$ssh\ssh_host_*", "$ssh\administrators_authorized_keys" -ErrorAction SilentlyContinue)
if ($left.Count) { throw "still present: $($left.FullName -join ', ')" }
Get-Service sshd, QEMU-GA | Format-Table Name, Status, StartType

Step 'SetupComplete.cmd and randomize-admin.ps1'
New-Item -ItemType Directory -Force -Path 'C:\Windows\Setup\Scripts' | Out-Null
# Set-Content writes CRLF line ends, which cmd.exe expects.
Get-Content -Path "$work\SetupComplete.cmd" | Set-Content -Path 'C:\Windows\Setup\Scripts\SetupComplete.cmd' -Encoding Ascii
Copy-Item -Path "$work\randomize-admin.ps1" -Destination 'C:\Windows\Setup\Scripts\randomize-admin.ps1' -Force

Step 'sysprep answer file (no password in it)'
$xml = Get-Content -Raw -Path "$work\sysprep-unattend.xml"
[xml]$xml | Out-Null
if ($xml -match '<(Administrator)?Password>|<Value>') { throw 'sysprep-unattend.xml must not carry a password' }
Set-Content -Path 'C:\Windows\System32\Sysprep\base-unattend.xml' -Value $xml -Encoding UTF8
# Setup's own copies of the install answer file (setup masks their passwords;
# nothing needs them again).
Remove-Item -Force -ErrorAction SilentlyContinue 'C:\Windows\Panther\unattend.xml', 'C:\Windows\Panther\Unattend\unattend.xml', 'C:\Windows\Panther\autounattend.xml'

Step 'no automatic logon'
$winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
Set-ItemProperty -Path $winlogon -Name AutoAdminLogon -Value '0'
foreach ($n in 'DefaultPassword', 'AutoLogonCount') {
    Remove-ItemProperty -Path $winlogon -Name $n -ErrorAction SilentlyContinue
}

Step 'event logs'
# Some logs cannot be cleared; Windows PowerShell turns a redirected native
# stderr line into a terminating error under 'Stop'.
$ErrorActionPreference = 'Continue'
foreach ($log in (& wevtutil.exe el)) { & wevtutil.exe cl "$log" 2>$null }
$ErrorActionPreference = 'Stop'
Write-Output 'finalized'
