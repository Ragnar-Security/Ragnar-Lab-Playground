# Installed by proxmox_base_windows.sh (finalize.ps1) as
# C:\Windows\Setup\Scripts\randomize-admin.ps1. The sysprep answer file runs it
# as SYSTEM in the specialize pass of every clone's first boot, before OOBE:
# it sets Administrator to a 32-character password drawn from the
# cryptographic RNG, generated here, held only in a SecureString and never
# output or stored. So no answer file carries a password, and no two clones
# share one. SetupComplete.cmd deletes this script when setup has finished.
$ErrorActionPreference = 'Stop'
$alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%^&*-_=+'
$limit = 256 - (256 % $alphabet.Length)   # reject values that would bias the pick
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
try {
    $byte = New-Object byte[] 1
    do {
        $secure = New-Object System.Security.SecureString
        $classes = @{}
        while ($secure.Length -lt 32) {
            $rng.GetBytes($byte)
            if ($byte[0] -lt $limit) {
                $c = $alphabet[$byte[0] % $alphabet.Length]
                $classes[$(if ([char]::IsUpper($c)) { 'U' } elseif ([char]::IsLower($c)) { 'L' } elseif ([char]::IsDigit($c)) { 'D' } else { 'S' })] = 1
                $secure.AppendChar($c)
            }
        }
    } until ($classes.Count -eq 4)
} finally {
    $rng.Dispose()
}
Set-LocalUser -Name 'Administrator' -Password $secure
