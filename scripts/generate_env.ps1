# Генерирует .env из .env.example для первого запуска (ТЗ 13: секреты не
# должны совпадать с публично известными значениями по умолчанию из репозитория).
# SECRET_KEY, RTSP_ENCRYPTION_KEY и ADMIN_PASSWORD переопределяются случайными
# значениями; остальные строки шаблона копируются как есть.
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$examplePath = Join-Path $root ".env.example"
$envPath = Join-Path $root ".env"

if (Test-Path $envPath) {
    Write-Host "[.env] уже существует, пропускаю генерацию"
    exit 0
}

function New-RandomHex([int]$bytes) {
    $buf = New-Object byte[] $bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($buf)
    -join ($buf | ForEach-Object { $_.ToString("x2") })
}

function New-FernetKey() {
    # Fernet-ключ: 32 случайных байта, base64 в url-safe алфавите (- и _), с '='.
    $buf = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($buf)
    $b64 = [Convert]::ToBase64String($buf)
    $b64.Replace('+', '-').Replace('/', '_')
}

function New-Password([int]$length) {
    $chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    $buf = New-Object byte[] $length
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($buf)
    -join ($buf | ForEach-Object { $chars[$_ % $chars.Length] })
}

$secretKey = New-RandomHex 32
$rtspKey = New-FernetKey
$adminPassword = New-Password 16

$lines = Get-Content -Path $examplePath
$out = foreach ($line in $lines) {
    if ($line -match '^SECRET_KEY=') { "SECRET_KEY=$secretKey" }
    elseif ($line -match '^RTSP_ENCRYPTION_KEY=') { "RTSP_ENCRYPTION_KEY=$rtspKey" }
    elseif ($line -match '^ADMIN_PASSWORD=') { "ADMIN_PASSWORD=$adminPassword" }
    else { $line }
}
Set-Content -Path $envPath -Value $out -Encoding utf8

Write-Host ""
Write-Host "=== .env создан со случайными секретами ==="
Write-Host "Логин администратора:  admin"
Write-Host "Пароль администратора: $adminPassword"
Write-Host "(сохранён в .env, при желании смените в интерфейсе после входа)"
Write-Host ""
