# Генерирует .env из .env.example для первого запуска (ТЗ 13: секреты не
# должны совпадать с публично известными значениями по умолчанию из репозитория).
# SECRET_KEY, RTSP_ENCRYPTION_KEY и ADMIN_PASSWORD переопределяются случайными
# значениями; остальные строки шаблона копируются как есть.
#
# ВАЖНО: этот файл обязан быть сохранён в UTF-8 С BOM.
# Windows PowerShell 5.1 (именно её вызывает start.bat командой `powershell`)
# читает .ps1 без BOM в ANSI-кодировке системы — на русской Windows это
# CP1251. Кириллица тогда декодируется побайтово, и буква 'ф' (UTF-8 D1 84)
# превращается в символ U+201E („), который парсер PowerShell считает
# закрывающей двойной кавычкой: строковый литерал обрывается на середине и
# падает разбор всего скрипта. Проверка на BOM есть в
# backend/tests/test_windows_bootstrap.py, чтобы регрессия не повторилась.
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$examplePath = Join-Path $root ".env.example"
$envPath = Join-Path $root ".env"

if (Test-Path $envPath) {
    Write-Host "[.env] уже существует, пропускаю генерацию"
    exit 0
}

function Get-RandomBytes([int]$count) {
    # RandomNumberGenerator::Create() + инстанс-метод GetBytes() есть и в
    # .NET Framework 4.x (на нём работает Windows PowerShell 5.1), и в
    # .NET 5+ (PowerShell 7). Статический ::Fill(), стоявший здесь раньше,
    # появился только в .NET Core 2.1 и на 5.1 падает с
    # MethodException — то есть скрипт не работал бы и после починки BOM.
    $buf = New-Object byte[] $count
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buf) } finally { $rng.Dispose() }
    # Запятая не даёт PowerShell развернуть массив в поток отдельных байт.
    ,$buf
}

function Get-RandomIndex([int]$upperBound) {
    # Отбраковка вместо простого остатка: 256 не делится нацело на длину
    # алфавита, поэтому `байт % длина` смещает выбор в пользу первых
    # символов. Берём 4 байта и отбрасываем значения из неполного хвоста.
    $max = [uint32]::MaxValue
    $limit = $max - ($max % [uint32]$upperBound) - 1
    while ($true) {
        $value = [BitConverter]::ToUInt32((Get-RandomBytes 4), 0)
        if ($value -le $limit) { return [int]($value % [uint32]$upperBound) }
    }
}

function New-RandomHex([int]$bytes) {
    -join ((Get-RandomBytes $bytes) | ForEach-Object { $_.ToString("x2") })
}

function New-FernetKey() {
    # Fernet-ключ: 32 случайных байта, base64 в url-safe алфавите (- и _), с '='.
    $b64 = [Convert]::ToBase64String((Get-RandomBytes 32))
    $b64.Replace('+', '-').Replace('/', '_')
}

# Алфавит без визуально неразличимых символов (I, O, l, 0, 1) — пароль
# придётся читать с экрана и вводить руками. Спецсимволы не используются
# сознательно: значение уходит в .env, который парсят и docker compose, и
# python-dotenv, и лишние кавычки/доллары там только создают проблемы.
$PasswordUpper = "ABCDEFGHJKLMNPQRSTUVWXYZ"
$PasswordLower = "abcdefghijkmnopqrstuvwxyz"
$PasswordDigits = "23456789"

function New-Password([int]$length) {
    # Парольная политика проекта (backend/app/schemas.py) требует минимум
    # 3 из 4 классов символов. Равномерная выборка из общего алфавита их не
    # гарантирует — на 16 символах примерно каждый девятый пароль недобирал
    # бы класс. Здесь по одному символу каждого из трёх классов берётся
    # явно, остальное добирается случайно.
    $all = $PasswordUpper + $PasswordLower + $PasswordDigits
    $chars = New-Object System.Collections.ArrayList
    [void]$chars.Add($PasswordUpper[(Get-RandomIndex $PasswordUpper.Length)])
    [void]$chars.Add($PasswordLower[(Get-RandomIndex $PasswordLower.Length)])
    [void]$chars.Add($PasswordDigits[(Get-RandomIndex $PasswordDigits.Length)])
    while ($chars.Count -lt $length) {
        [void]$chars.Add($all[(Get-RandomIndex $all.Length)])
    }
    # Перемешивание Фишера-Йейтса: иначе первые три позиции всегда были бы
    # предсказуемого класса (заглавная, строчная, цифра).
    for ($i = $chars.Count - 1; $i -gt 0; $i--) {
        $j = Get-RandomIndex ($i + 1)
        $tmp = $chars[$i]
        $chars[$i] = $chars[$j]
        $chars[$j] = $tmp
    }
    -join $chars
}

$secretKey = New-RandomHex 32
$rtspKey = New-FernetKey
$adminPassword = New-Password 16

# -Encoding UTF8 обязателен: .env.example лежит в UTF-8 без BOM и содержит
# кириллические комментарии, а Get-Content в PowerShell 5.1 по умолчанию
# читает в ANSI — комментарии уехали бы в .env мозаикой.
$lines = Get-Content -Path $examplePath -Encoding UTF8
$out = foreach ($line in $lines) {
    if ($line -match '^SECRET_KEY=') { "SECRET_KEY=$secretKey" }
    elseif ($line -match '^RTSP_ENCRYPTION_KEY=') { "RTSP_ENCRYPTION_KEY=$rtspKey" }
    elseif ($line -match '^ADMIN_PASSWORD=') { "ADMIN_PASSWORD=$adminPassword" }
    else { $line }
}

# Пишем через .NET, а не Set-Content: в Windows PowerShell 5.1
# `Set-Content -Encoding utf8` добавляет BOM (в PowerShell 7 — нет), и этот
# BOM приклеился бы к первому ключу .env как ﻿POSTGRES_USER.
#
# Переносы строк — CRLF, намеренно. Этот .env читает только docker compose на
# самой Windows (в контейнеры значения приходят через секцию environment:,
# а не файлом), поэтому родная для Windows CRLF безопаснее: её одинаково
# понимают и парсер compose, и findstr, которым start.bat проверяет файл на
# оставшиеся дефолтные секреты.
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($envPath, (($out -join "`r`n") + "`r`n"), $utf8NoBom)

Write-Host ""
Write-Host "=== .env создан со случайными секретами ==="
Write-Host "Логин администратора:  admin"
Write-Host "Пароль администратора: $adminPassword"
Write-Host "(сохранён в .env, при желании смените в интерфейсе после входа)"
Write-Host ""
