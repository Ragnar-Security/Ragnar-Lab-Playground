# Build step "prepare" of the Windows base (proxmox_base_windows.sh), run as
# SYSTEM through step.ps1. Installs OpenSSH Server and sets it up the way the
# image builder uses it: key login for Administrator through
# C:\ProgramData\ssh\administrators_authorized_keys, no password login, the
# default shell left as cmd.exe, tcp/22 open on every firewall profile.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
function Step($m) { Write-Output "== $m" }
$ssh = 'C:\ProgramData\ssh'

Step 'OS'
$os = Get-CimInstance Win32_OperatingSystem
$ubr = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').UBR
Write-Output "$($os.Caption) $($os.Version).$ubr"

Step 'OpenSSH Server'
$cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'
for ($try = 1; $cap.State -ne 'Installed' -and $try -le 3; $try++) {
    Write-Output "Add-WindowsCapability OpenSSH.Server (try $try)"
    try { Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' | Out-Null }
    catch { Write-Output "  failed: $_"; Start-Sleep -Seconds 30 }
    $cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'
}
if ($cap.State -ne 'Installed') { throw "OpenSSH.Server is $($cap.State) after 3 tries" }
Write-Output "OpenSSH.Server: $($cap.State); $((Get-Item C:\Windows\System32\OpenSSH\sshd.exe).VersionInfo.ProductVersion)"

# The first start writes sshd_config and the host keys.
Start-Service sshd
Start-Sleep -Seconds 3
if (-not (Test-Path "$ssh\ssh_host_ed25519_key")) { throw 'sshd started but wrote no host keys' }

# The base ships without host keys (finalize.ps1 deletes them), so each clone
# must get its own when sshd first starts. Proven here, on this sshd.
Stop-Service sshd
Remove-Item -Force "$ssh\ssh_host_*"
Start-Service sshd
Start-Sleep -Seconds 3
if (-not (Test-Path "$ssh\ssh_host_ed25519_key")) { throw 'sshd does not regenerate missing host keys' }
Write-Output 'sshd regenerates missing host keys: yes'

$cfg = "$ssh\sshd_config"
$text = Get-Content -Raw -Path $cfg
if ($text -notmatch '(?m)^Match Group administrators\s*$' -or
    $text -notmatch '(?m)^\s*AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys\s*$') {
    throw "$cfg does not read administrators' keys from administrators_authorized_keys"
}
# First value wins in sshd_config: set before any Match block. Set-Content
# rewrites the existing file, keeping its ACL.
if ($text -notmatch '(?m)^PasswordAuthentication no\s*$') {
    Set-Content -Path $cfg -Value ("# proxmox_base_windows.sh: key login only`r`nPasswordAuthentication no`r`n" + $text) -Encoding Ascii -NoNewline
}
Restart-Service sshd
$effective = & C:\Windows\System32\OpenSSH\sshd.exe -T | Where-Object { $_ -match '^(passwordauthentication|pubkeyauthentication) ' }
Write-Output "sshd -T: $($effective -join '; ')"
if ($effective -notcontains 'passwordauthentication no') { throw 'sshd -T does not report passwordauthentication no' }
if ((Get-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -ErrorAction SilentlyContinue)) {
    throw 'HKLM:\SOFTWARE\OpenSSH DefaultShell is set; the image builder expects cmd.exe'
}

Step 'firewall tcp/22'
$rule = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
if (-not $rule) {
    New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH SSH Server (sshd)' -Enabled True `
        -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 -Profile Any | Out-Null
} else {
    Set-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -Enabled True -Profile Any -Action Allow
}
Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' | Format-List Name, Enabled, Profile, Direction, Action

Step 'guest agent'
$ga = Get-Service QEMU-GA
Write-Output "QEMU-GA: $($ga.Status), $($ga.StartType)"
if ($ga.StartType -ne 'Automatic') { throw 'QEMU-GA does not start automatically' }

Step 'misc'
& powercfg.exe /hibernate off
Get-ScheduledTask -TaskName 'ServerManager' -ErrorAction SilentlyContinue | Disable-ScheduledTask | Out-Null

Step 'licence'
& cscript.exe //nologo C:\Windows\System32\slmgr.vbs /dlv
