<#
.SYNOPSIS
    冒烟测试：把 dist\PDFTool\PDFTool.exe 真起一次，验证健康检查与首页。

.DESCRIPTION
    这一步回答的是「打出来的东西到底能不能用」，所以三条都不能省：

    1. 用**真进程**而不是导入模块——打包后才暴露的问题（漏 hidden-import、
       漏 --add-data、DLL 找不到）只有真起一次才会显形。
    2. 用**临时数据目录**，不污染开发时用的 var\。
    3. 断言 `/api/health` 的 status=ok 和首页的 200 + DOCTYPE：
       前者证明服务活着，后者证明静态资源真的进了包。

    任一步失败都以退出码 1 结束，并打印进程输出；成功退出码 0。
    进程在 finally 里强制结束，不留后台残留。

.PARAMETER ExePath
    被测 exe，默认 dist\PDFTool\PDFTool.exe。

.PARAMETER Port
    指定端口，默认自动找一个空闲端口。

.PARAMETER TimeoutSeconds
    等待健康检查的上限，默认 60 秒（首次启动要加载 PyMuPDF，比源码态慢）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\smoke_test.ps1
#>
[CmdletBinding()]
param(
    [string]$ExePath,
    [int]$Port = 0,
    [int]$TimeoutSeconds = 60
)

$ErrorActionPreference = 'Stop'
# 进度条会在日志里留下一堆回车重绘的碎片，冒烟脚本不需要它
$ProgressPreference = 'SilentlyContinue'
# 中文在管道/重定向下也按 UTF-8 输出，避免父进程按 ANSI 解成乱码
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }

$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not $ExePath) {
    $ExePath = Join-Path $RepoRoot 'dist\PDFTool\PDFTool.exe'
}

function Get-FreePort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    $listener.Stop()
    return $port
}

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

$proc = $null
$workDir = $null
$failure = $null
$outTask = $null
$errTask = $null
$stdoutText = ''
$stderrText = ''

try {
    if (-not (Test-Path -LiteralPath $ExePath)) {
        throw "找不到 $ExePath。先运行 scripts\build.ps1 生成产物。"
    }
    if ($Port -le 0) { $Port = Get-FreePort }

    $workDir = Join-Path ([System.IO.Path]::GetTempPath()) ("pdftool-smoke-" + [guid]::NewGuid().ToString('N'))
    $varDir = Join-Path $workDir 'var'
    New-Item -ItemType Directory -Path $varDir -Force | Out-Null

    Write-Step "启动 $ExePath（端口 $Port，数据目录 $varDir）"

    # 刻意不用 Start-Process：它会把父进程环境变量拷进一个大小写不敏感的
    # StringDictionary，而某些环境里同时存在 http_proxy 和 HTTP_PROXY，
    # 拷贝时直接抛「已添加项」——和被测程序毫无关系的失败。
    # 直接用 .NET Process（不访问 EnvironmentVariables）绕开这一层。
    $env:PYTHONIOENCODING = 'utf-8'  # 让子进程按 UTF-8 写 stdout，日志才读得懂
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $ExePath
    $psi.WorkingDirectory = Split-Path -Parent $ExePath
    $psi.Arguments = '--port {0} --var-dir "{1}" --no-browser' -f $Port, $varDir
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    [void]$proc.Start()
    # 异步读，避免管道缓冲区写满把子进程卡死（同步读要等进程退出才拿得到）
    $outTask = $proc.StandardOutput.ReadToEndAsync()
    $errTask = $proc.StandardError.ReadToEndAsync()

    $baseUrl = "http://127.0.0.1:$Port"
    $healthUrl = "$baseUrl/api/health"

    Write-Step "等待 $healthUrl"
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $payload = $null
    $lastError = '尚未发起请求'
    while ((Get-Date) -lt $deadline) {
        if ($proc.HasExited) {
            throw "进程在服务就绪前退出（退出码 $($proc.ExitCode)）"
        }
        try {
            $payload = Invoke-RestMethod -Uri $healthUrl -Method Get -TimeoutSec 3
            break
        }
        catch {
            $lastError = $_.Exception.Message
            Start-Sleep -Milliseconds 300
        }
    }
    if ($null -eq $payload) {
        throw "$TimeoutSeconds 秒内 $healthUrl 没有返回 200：$lastError"
    }

    if ($payload.status -ne 'ok') {
        throw "健康检查 status 不是 ok，而是 '$($payload.status)'"
    }
    if (-not $payload.version) {
        throw "健康检查响应里没有 version"
    }
    if (-not $payload.generation_id) {
        throw "健康检查响应里没有 generation_id"
    }
    Write-Host ("健康检查通过：status={0} version={1} generation={2}" -f `
        $payload.status, $payload.version, $payload.generation_id)

    Write-Step "请求首页 $baseUrl/"
    $index = Invoke-WebRequest -Uri "$baseUrl/" -UseBasicParsing -TimeoutSec 10
    if ($index.StatusCode -ne 200) {
        throw "首页返回 $($index.StatusCode)，期望 200"
    }
    if ($index.Content -notmatch '(?i)<!doctype\s+html') {
        throw "首页内容不像 HTML 页面（静态资源可能没打进包）"
    }
    Write-Host ("首页通过：{0} 字节" -f $index.RawContentLength)

    Write-Host ""
    Write-Host "冒烟测试通过。" -ForegroundColor Green
}
catch {
    $failure = $_.Exception.Message
}
finally {
    if ($null -ne $proc) {
        try {
            if (-not $proc.HasExited) {
                $proc.Kill()
                [void]$proc.WaitForExit(10000)
            }
        }
        catch {
            Write-Host "警告：结束进程失败：$($_.Exception.Message)" -ForegroundColor Yellow
        }
        if ($proc.HasExited) {
            try { $stdoutText = $outTask.Result } catch { }
            try { $stderrText = $errTask.Result } catch { }
        }
    }
}

if ($failure) {
    Write-Host ""
    Write-Host "冒烟测试失败：$failure" -ForegroundColor Red
    if ($stdoutText) {
        Write-Host ""
        Write-Host "--- 进程 stdout ---" -ForegroundColor Yellow
        Write-Host $stdoutText
    }
    if ($stderrText) {
        Write-Host ""
        Write-Host "--- 进程 stderr ---" -ForegroundColor Yellow
        Write-Host $stderrText
    }
    exit 1
}

if ($null -ne $workDir -and (Test-Path -LiteralPath $workDir)) {
    Remove-Item -LiteralPath $workDir -Recurse -Force -ErrorAction SilentlyContinue
}
exit 0
