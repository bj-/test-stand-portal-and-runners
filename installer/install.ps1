# Windows bootstrap; -CheckOnly and -Plan never install or write files.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Workspace,
    [Parameter(Mandatory = $true)][string]$PortalSource,
    [string]$Name = '',
    [string]$Port = '',
    [switch]$CreateAdmin,
    [switch]$CheckOnly,
    [switch]$Plan
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$script:Python = $null
$Launcher = Join-Path $PSScriptRoot '../local/portal_local.py'

function Quote-PowerShell([string]$Value) { return "'" + $Value.Replace("'", "''") + "'" }
function Show-Rerun {
    $command = "powershell -NoProfile -ExecutionPolicy Bypass -File $(Quote-PowerShell $PSCommandPath) -Workspace $(Quote-PowerShell $Workspace) -PortalSource $(Quote-PowerShell $PortalSource)"
    if ($Name) { $command += " -Name $(Quote-PowerShell $Name)" }
    if ($Port) { $command += " -Port $(Quote-PowerShell $Port)" }
    if ($CreateAdmin) { $command += ' -CreateAdmin' }
    Write-Host "Повторить: $command"
}
function Stop-ForContinuation([string]$Message) {
    Write-Host $Message
    Show-Rerun
    exit 20
}
function Refresh-ProcessPath {
    $paths = @([Environment]::GetEnvironmentVariable('Path', 'Machine'),
               [Environment]::GetEnvironmentVariable('Path', 'User'), $env:Path,
               (Join-Path $env:LOCALAPPDATA 'Programs/Python/Python312'),
               (Join-Path $env:LOCALAPPDATA 'Programs/Python/Launcher'),
               (Join-Path $env:LOCALAPPDATA 'Programs/DockerDesktop/resources/bin'),
               (Join-Path $env:ProgramFiles 'Docker/Docker/resources/bin'))
    $env:Path = (($paths -join ';').Split(';') | Where-Object { $_ } | Select-Object -Unique) -join ';'
}
function Find-Python {
    foreach ($commandName in @('py.exe', 'python.exe', 'python3.exe')) {
        $command = Get-Command $commandName -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        if ($commandName -ne 'py.exe' -and $command.Source -match '\\WindowsApps\\') { continue }
        $prefix = @()
        if ($commandName -eq 'py.exe') { $prefix = @('-3') }
        try {
            $null = & $command.Source @prefix -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' 2>$null
            if ($LASTEXITCODE -eq 0) {
                $script:Python = [pscustomobject]@{ Executable = $command.Source; Prefix = $prefix }
                return $true
            }
        } catch { }
    }
    $script:Python = $null
    return $false
}
function Invoke-Python([string[]]$Arguments) {
    $prefix = $script:Python.Prefix
    $executable = $script:Python.Executable
    & $executable @prefix @Arguments
    if ($LASTEXITCODE -eq 20) { Stop-ForContinuation 'Портал ещё не прошёл health; проверьте logs и повторите installer. Данные сохранены.' }
    if ($LASTEXITCODE -ne 0) { throw "Команда Python завершилась с кодом $LASTEXITCODE. Проверьте сообщение выше; повторный запуск сохраняет данные." }
}
function Test-WorkingDocker {
    if (-not $script:Python) { return $false }
    $prefix = $script:Python.Prefix
    $executable = $script:Python.Executable
    & $executable @prefix (Join-Path $PSScriptRoot 'probe_docker.py')
    return ($LASTEXITCODE -eq 0)
}
function Install-WingetPackage([string]$Id) {
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        throw 'Не найден winget. Установите/обновите App Installer из Microsoft Store, затем повторите эту же команду.'
    }
    Write-Host "Установка $Id через winget. Соглашения и запросы установщика подтверждаете самостоятельно."
    & winget.exe install --exact --id $Id --source winget
    if ($LASTEXITCODE -eq 3010 -or $LASTEXITCODE -eq 1641) { Stop-ForContinuation 'Установка требует перезагрузки Windows. Перезагрузите ПК и повторите команду.' }
    if ($LASTEXITCODE -ne 0) { throw "winget не завершил установку $Id (код $LASTEXITCODE). Исправьте причину по сообщению winget и повторите команду." }
    Refresh-ProcessPath
}
function Ensure-WSL {
    $wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
    if ($wsl) {
        try {
            $null = & $wsl.Source --status 2>$null
            if ($LASTEXITCODE -eq 0) { return }
        } catch { }
    }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Stop-ForContinuation 'Для WSL откройте PowerShell от имени администратора, выполните wsl --install --no-distribution, перезагрузите ПК при запросе и повторите installer в обычном терминале.'
    }
    if (-not $wsl) { throw 'wsl.exe отсутствует. Обновите Windows до поддерживаемой версии и включите WSL по инструкции Microsoft.' }
    & $wsl.Source --install --no-distribution
    if ($LASTEXITCODE -notin @(0, 3010, 1641)) { throw "Не удалось установить WSL (код $LASTEXITCODE); проверьте сообщение wsl.exe." }
    Stop-ForContinuation 'WSL установлен/включён. Если Windows запрашивает перезагрузку, перезагрузите ПК; затем повторите installer в обычном терминале.'
}

try {
    if ($CheckOnly -and $Plan) { throw 'Выберите только один режим: -CheckOnly или -Plan' }
    if (-not (Test-Path -LiteralPath (Join-Path $PortalSource 'docker-compose.yaml') -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $PortalSource 'Dockerfile') -PathType Leaf)) { throw 'PortalSource должен содержать docker-compose.yaml и Dockerfile продукта' }
    if ($Name -and $Name -cnotmatch '^[a-z0-9][a-z0-9_-]{0,39}$') { throw 'Некорректное Name: 1–40 букв a-z, цифр, _ или -, начинается с буквы/цифры' }
    if ($Port -and ($Port -notmatch '^\d{4,5}$' -or [int]$Port -lt 1024 -or [int]$Port -gt 65535)) { throw 'Port должен быть от 1024 до 65535' }
    if ($Plan) {
        Write-Host 'План: Python >=3.12 и Git (winget Python.Python.3.12, Git.Git при отсутствии); WSL; Docker Desktop (Docker.DockerDesktop при отсутствии).'
        Write-Host 'Первый запуск Docker и лицензионное соглашение завершаете самостоятельно. При необходимости перезагрузки выход 20, без автоматического reboot.'
        Write-Host 'Затем init, doctor, up через launcher; ожидание /api/health; автоматическая admin-init: admin/admin только при пустой базе пользователей, существующие учётные записи сохраняются. Затем agent-init и agent-start: токен host-агента сохраняется, фоновый worker запускается автоматически.'
        Show-Rerun
        exit 0
    }
    Refresh-ProcessPath
    $pythonFound = Find-Python
    if ($CheckOnly) {
        Write-Host 'При установке после health выполняется автоматическая admin-init; сейчас база пользователей не изменяется и host-агент не запускается (agent-init/agent-start только при установке).'
        $missing = $false
        if ($pythonFound) { Write-Host 'Python >=3.12: OK' } else { Write-Host 'Python >=3.12: отсутствует'; $missing = $true }
        if (Get-Command git.exe -ErrorAction SilentlyContinue) { Write-Host 'Git: OK' } else { Write-Host 'Git: отсутствует'; $missing = $true }
        if (Test-WorkingDocker) { Write-Host 'Docker Compose + Linux daemon: OK' } else { Write-Host 'Docker Compose/Linux daemon: недоступен'; $missing = $true }
        if ($missing) { exit 1 } else { exit 0 }
    }
    if (-not $pythonFound) {
        Install-WingetPackage 'Python.Python.3.12'
        if (-not (Find-Python)) { Stop-ForContinuation 'Python установлен, но ещё не виден. Откройте новый обычный терминал.' }
    }
    if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) { Install-WingetPackage 'Git.Git' }
    if (-not (Test-WorkingDocker)) {
        Ensure-WSL
        if (-not (Get-Command docker.exe -ErrorAction SilentlyContinue)) { Install-WingetPackage 'Docker.DockerDesktop' }
        $desktopPaths = @((Join-Path $env:LOCALAPPDATA 'Programs/DockerDesktop/Docker Desktop.exe'),
                          (Join-Path $env:ProgramFiles 'Docker/Docker/Docker Desktop.exe'))
        $desktop = $desktopPaths | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
        if ($desktop) {
            Write-Host 'Запускаю Docker Desktop. Откройте Docker и самостоятельно завершите первый запуск/соглашение; выберите Linux containers.'
            Start-Process -FilePath $desktop -WindowStyle Hidden | Out-Null
        }
    }
    Write-Host 'Ожидаю Docker daemon (до 120 секунд)…'
    $deadline = [DateTime]::UtcNow.AddSeconds(120)
    while (-not (Test-WorkingDocker)) {
        if ([DateTime]::UtcNow -ge $deadline) { Stop-ForContinuation 'Docker пока недоступен. Откройте Docker Desktop, завершите первый запуск и переключитесь в Linux containers.' }
        Start-Sleep -Seconds 3
    }
    $initArguments = @($Launcher, '--workspace', $Workspace, '--portal-source', $PortalSource, 'init')
    if ($Name) { $initArguments += @('--name', $Name) }
    if ($Port) { $initArguments += @('--port', $Port) }
    Invoke-Python $initArguments
    Invoke-Python @($Launcher, '--workspace', $Workspace, 'doctor')
    Invoke-Python @($Launcher, '--workspace', $Workspace, 'up')
    Invoke-Python @((Join-Path $PSScriptRoot 'wait_health.py'), '--workspace', $Workspace, '--timeout', '180')
    Invoke-Python @($Launcher, '--workspace', $Workspace, 'admin-init')
    Invoke-Python @($Launcher, '--workspace', $Workspace, 'agent-init')
    Invoke-Python @($Launcher, '--workspace', $Workspace, 'agent-start')
    exit 0
} catch {
    Write-Host "Ошибка: $($_.Exception.Message)"
    Show-Rerun
    exit 1
}
