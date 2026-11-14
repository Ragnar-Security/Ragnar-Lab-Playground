# Run one build step (C:\basebuild\<Name>.ps1) as SYSTEM in a scheduled task,
# so it is not bound by the guest agent's command timeout.
#   step.ps1 -Start -Name <n>   (through the agent) register and start the task
#   step.ps1 -Name <n>          (the task itself) run the step
# The step's output goes to <n>.out and <n>.err; <n>.status reads "running",
# then "exit <code>". proxmox_base_windows.sh polls the status file.
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-z_]+$')][string]$Name,
    [switch]$Start
)
$ErrorActionPreference = 'Stop'
$work = 'C:\basebuild'
$status = "$work\$Name.status"

if ($Start) {
    Set-Content -Path $status -Value 'running' -Encoding Ascii
    Remove-Item -Force -ErrorAction SilentlyContinue "$work\$Name.out", "$work\$Name.err"
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File $work\step.ps1 -Name $Name"
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName "basebuild-$Name" -Action $action -Principal $principal `
        -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName "basebuild-$Name"
    exit 0
}

$rc = 1
try {
    # WaitForExit, not -Wait: -Wait also waits for every descendant process.
    # Reading Handle first keeps ExitCode readable after the exit.
    $p = Start-Process -FilePath 'powershell.exe' -NoNewWindow -PassThru `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "$work\$Name.ps1") `
        -RedirectStandardOutput "$work\$Name.out" -RedirectStandardError "$work\$Name.err"
    $null = $p.Handle
    $p.WaitForExit()
    $rc = $p.ExitCode
} catch {
    Add-Content -Path "$work\$Name.err" -Value "step.ps1: $_"
}
Set-Content -Path $status -Value "exit $rc" -Encoding Ascii
