# First logon of the Windows base install (proxmox_base_windows.sh), run from
# the run's ISO by the answer file's FirstLogonCommands. Copies the build
# scripts to C:\basebuild, installs the virtio drivers and the QEMU guest
# agent from the virtio-win ISO, and starts the agent: everything after this
# runs through the agent. Log: C:\basebuild\firstlogon.log.
$ErrorActionPreference = 'Stop'
$work = 'C:\basebuild'
New-Item -ItemType Directory -Force -Path $work | Out-Null
Start-Transcript -Path "$work\firstlogon.log" -Append | Out-Null
try {
    $src = Split-Path -Parent $MyInvocation.MyCommand.Path
    Copy-Item -Path "$src\*" -Destination $work -Force
    Write-Output "copied the build scripts from $src"

    $virtio = Get-PSDrive -PSProvider FileSystem |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.Root 'virtio-win-gt-x64.msi') } |
        Select-Object -First 1
    if (-not $virtio) { throw 'no drive holds virtio-win-gt-x64.msi (is the virtio-win ISO attached?)' }
    $v = $virtio.Root
    Write-Output "virtio-win ISO at $v"

    # The publisher of the driver catalogs is trusted first, so a quiet
    # install never waits on a driver-trust prompt.
    $signer = (Get-AuthenticodeSignature -FilePath (Join-Path $v 'NetKVM\2k22\amd64\netkvm.cat')).SignerCertificate
    if ($signer) {
        $store = New-Object System.Security.Cryptography.X509Certificates.X509Store('TrustedPublisher', 'LocalMachine')
        $store.Open('ReadWrite'); $store.Add($signer); $store.Close()
        Write-Output "trusted publisher: $($signer.Subject)"
    }

    foreach ($msi in @('virtio-win-gt-x64.msi', 'guest-agent\qemu-ga-x86_64.msi')) {
        $path = Join-Path $v $msi
        $p = Start-Process -FilePath msiexec.exe -ArgumentList @('/i', "`"$path`"", '/qn', '/norestart', '/l*v', "`"$work\$(Split-Path -Leaf $msi).log`"") -Wait -PassThru
        Write-Output "$msi exit $($p.ExitCode)"
        if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) { throw "$msi failed with exit $($p.ExitCode)" }
    }

    Set-Service -Name QEMU-GA -StartupType Automatic
    Start-Service -Name QEMU-GA
    Write-Output "QEMU-GA: $((Get-Service QEMU-GA).Status)"
    Set-Content -Path "$work\firstlogon.done" -Value 'ok' -Encoding Ascii
} catch {
    Write-Output "firstlogon.ps1 FAILED: $_"
    Set-Content -Path "$work\firstlogon.done" -Value "failed: $_" -Encoding Ascii
    exit 1
} finally {
    Stop-Transcript | Out-Null
}
