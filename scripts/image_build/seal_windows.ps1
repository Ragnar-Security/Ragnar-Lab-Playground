# Seal a Windows build VM just before its image is saved.
#
# Removes the uploaded payload and the build key, sets Administrator to a random
# 32-character password that is never written anywhere, and (unless -KeepSsh)
# disables sshd. Exits 1 if the payload directory survives, so the build stops
# before the image is saved.
param(
    [switch]$KeepSsh
)
$ErrorActionPreference = 'Stop'

# Fail closed: an absent path is fine, a present path that cannot be deleted
# stops the script (non-zero exit) so the image is never saved with it.
$sealPaths = @('C:\image-build', 'C:\ProgramData\ssh\administrators_authorized_keys')
foreach ($p in $sealPaths) {
    if (Test-Path -LiteralPath $p) {
        Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction Stop
    }
}

# 32 characters from an alphabet with every character class, drawn from the
# cryptographic RNG. The value is held in a SecureString and never output.
$alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%^&*-_=+'
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$secure = New-Object System.Security.SecureString
try {
    $byte = New-Object byte[] 1
    $limit = 256 - (256 % $alphabet.Length)   # reject values that would bias the pick
    while ($secure.Length -lt 32) {
        $rng.GetBytes($byte)
        if ($byte[0] -lt $limit) { $secure.AppendChar($alphabet[$byte[0] % $alphabet.Length]) }
    }
} finally {
    $rng.Dispose()
}
Set-LocalUser -Name 'Administrator' -Password $secure

if (-not $KeepSsh) {
    Set-Service sshd -StartupType Disabled
}

foreach ($p in $sealPaths) {
    if (Test-Path -LiteralPath $p) {
        [Console]::Error.WriteLine("seal_windows.ps1: $p still exists after removal")
        exit 1
    }
}
