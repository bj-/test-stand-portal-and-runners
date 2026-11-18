# Windows PowerShell 5.1 compatible; Plan/CheckOnly never run the example.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Workspace,
    [Parameter(Mandatory = $true)][string]$PortalSource,
    [string]$Name = '',
    [string]$Port = '',
    [switch]$CreateAdmin,
    [switch]$Retry,
    [switch]$Plan,
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
function Quote-PowerShell([string]$Value) { return "'" + $Value.Replace("'", "''") + "'" }
function Show-Rerun {
    $command = "powershell -NoProfile -ExecutionPolicy Bypass -File $(Quote-PowerShell $PSCommandPath) -Workspace $(Quote-PowerShell $Workspace) -PortalSource $(Quote-PowerShell $PortalSource)"
    if ($Name) { $command += " -Name $(Quote-PowerShell $Name)" }
    if ($Port) { $command += " -Port $(Quote-PowerShell $Port)" }
    if ($CreateAdmin) { $command += ' -CreateAdmin' }
    if ($Retry) { $command += ' -Retry' }
    Write-Host "Повторить: $command"
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
try {
    if ($Plan -and $CheckOnly) { throw 'Выберите только один режим: -Plan или -CheckOnly' }
    $installerArguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
        (Join-Path $PSScriptRoot 'installer/install.ps1'), '-Workspace', (Join-Path $Workspace 'portal'), '-PortalSource', $PortalSource)
    if ($Name) { $installerArguments += @('-Name', $Name) }
    if ($Port) { $installerArguments += @('-Port', $Port) }
    if ($CreateAdmin) { $installerArguments += '-CreateAdmin' }
    if ($Plan) { $installerArguments += '-Plan' }
    if ($CheckOnly) { $installerArguments += '-CheckOnly' }
    if ($Plan -or $CheckOnly) {
        $retryText = ''
        if ($Retry) { $retryText = ' --retry' }
        Write-Host "План примера: installer/deploy_demo.py --workspace $(Quote-PowerShell (Join-Path $Workspace 'example'))$retryText (после успешной установки портала; сейчас не выполняется)."
    }
    # The installer exits explicitly, so isolate it from this orchestration process.
    $shell = Join-Path $PSHOME 'powershell.exe'
    if (-not (Test-Path -LiteralPath $shell -PathType Leaf)) { $shell = Join-Path $PSHOME 'pwsh.exe' }
    & $shell @installerArguments
    $status = $LASTEXITCODE
    if ($status -ne 0) {
        if ($status -eq 20) { Write-Host 'Завершите действие, указанное installer, затем продолжите всей командой развёртывания.' }
        Show-Rerun
        exit $status
    }
    if ($Plan -or $CheckOnly) { exit 0 }
    Refresh-ProcessPath
    $python = $null
    foreach ($commandName in @('py.exe', 'python.exe', 'python3.exe')) {
        $command = Get-Command $commandName -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        if ($commandName -ne 'py.exe' -and $command.Source -match '\\WindowsApps\\') { continue }
        $prefixes = @('')
        if ($commandName -eq 'py.exe') { $prefixes = @('-3.12', '-3') }
        foreach ($version in $prefixes) {
            $prefix = @()
            if ($version) { $prefix = @($version) }
            try {
                $null = & $command.Source @prefix -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' 2>$null
                if ($LASTEXITCODE -eq 0) {
                    $python = [pscustomobject]@{ Executable = $command.Source; Prefix = $prefix }
                    break
                }
            } catch { }
        }
        if ($python) { break }
    }
    if (-not $python) {
        Write-Host 'Python >=3.12 установлен, но недоступен. Откройте новый терминал и повторите команду.'
        Show-Rerun
        exit 20
    }
    $demoArguments = @((Join-Path $PSScriptRoot 'installer/deploy_demo.py'), '--workspace', (Join-Path $Workspace 'example'))
    if ($Retry) { $demoArguments += '--retry' }
    $prefix = $python.Prefix
    & $python.Executable @prefix @demoArguments
    $status = $LASTEXITCODE
    if ($status -ne 0) { Show-Rerun }
    exit $status
} catch {
    Write-Host "Ошибка: $($_.Exception.Message)"
    Show-Rerun
    exit 1
}
