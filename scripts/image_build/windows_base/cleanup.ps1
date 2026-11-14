# Build step "cleanup" of the Windows base (proxmox_base_windows.sh), run as
# SYSTEM through step.ps1 after the last update round: drops superseded
# components and the update download cache, then asks for a retrim so the
# template's zvol can drop freed blocks.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
# DISM's progress bar arrives as hundreds of lines; keep the rest.
& Dism.exe /Online /Cleanup-Image /StartComponentCleanup /ResetBase /NoRestart |
    Where-Object { $_ -notmatch '^\s*\[=*' -and $_.Trim() }
if ($LASTEXITCODE -ne 0) { throw "DISM component cleanup exited $LASTEXITCODE" }
Stop-Service -Name wuauserv, bits -Force
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue 'C:\Windows\SoftwareDistribution\Download\*'
Start-Service -Name wuauserv
# Best effort: the virtio-scsi disk may not report thin provisioning, and
# Windows then refuses a retrim ("Not Supported").
try { Optimize-Volume -DriveLetter C -ReTrim -ErrorAction Stop; Write-Output 'retrim: done' }
catch { Write-Output "retrim: not done ($($_.Exception.Message.Trim()))" }
$ubr = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').UBR
Write-Output "OS build after updates: $([Environment]::OSVersion.Version.Build).$ubr"
Get-PSDrive C | Format-List Used, Free
