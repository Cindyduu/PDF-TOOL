<#
.SYNOPSIS
    把「PDF 自动设置工具」打成 dist\PDFTool\PDFTool.exe（onedir 形态）。

.DESCRIPTION
    只用 PyInstaller 一个工具，不引额外打包框架。三条约定：

    1. onedir 而不是 onefile。onefile 每次启动都要把上百 MB 的 PyMuPDF/pikepdf
       解包到临时目录，本机单用户工具没必要付这个启动代价。
    2. 静态资源必须 --add-data 显式带上。PyInstaller 不会因为 `app/static`
       在源码树里就自动收集它，漏了的表现是首页 404（`Settings.static_dir`
       已经做了 `sys._MEIPASS` 兼容，但前提是文件真被打进去了）。
    3. 许可证清单是产物的一部分，不是文档的一部分。PyMuPDF 是 AGPL-3.0，
       分发二进制时必须随附许可证，所以这里生成 THIRD-PARTY-LICENSES.txt。

.PARAMETER SkipInstall
    跳过「检查并安装 PyInstaller」这一步（离线或已确认装好时用）。

.PARAMETER Python
    指定解释器路径。默认依次尝试：`$env:PDFTOOL_PYTHON` → PATH 里的 `python` → `py -3`。
    开发机上 python 常常只在某个 venv 里，不在 PATH 上，所以留了这个口子。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\build.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\build.ps1 -Python C:\venv\Scripts\python.exe
#>
[CmdletBinding()]
param(
    [switch]$SkipInstall,
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'
# 中文在管道/重定向下也按 UTF-8 输出，避免父进程按 ANSI 解成乱码
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }

$RepoRoot = Split-Path -Parent $PSScriptRoot
$DistRoot = Join-Path $RepoRoot 'dist'
$BundleDir = Join-Path $DistRoot 'PDFTool'
$ExePath = Join-Path $BundleDir 'PDFTool.exe'

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Resolve-Python {
    param([string]$Explicit)

    if ($Explicit) {
        if (-not (Test-Path -LiteralPath $Explicit)) {
            throw "指定的 Python 不存在：$Explicit"
        }
        return $Explicit
    }
    if ($env:PDFTOOL_PYTHON) {
        if (-not (Test-Path -LiteralPath $env:PDFTOOL_PYTHON)) {
            throw "环境变量 PDFTOOL_PYTHON 指向的文件不存在：$($env:PDFTOOL_PYTHON)"
        }
        return $env:PDFTOOL_PYTHON
    }
    $candidate = Get-Command python -ErrorAction SilentlyContinue
    if ($candidate) { return $candidate.Source }
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) { return $launcher.Source }
    throw "找不到 python。请安装 Python 3.11+，或用 -Python 指定解释器路径，或设置环境变量 PDFTOOL_PYTHON。"
}

Push-Location $RepoRoot
try {
    $Python = Resolve-Python -Explicit $Python
    Write-Step "Python: $Python"
    & $Python -c "import sys; print(sys.version)"
    if ($LASTEXITCODE -ne 0) { throw "python 无法运行（退出码 $LASTEXITCODE）" }

    # ---- 1. PyInstaller --------------------------------------------------
    if (-not $SkipInstall) {
        Write-Step "检查 PyInstaller"
        & $Python -c "import PyInstaller" 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "未安装，开始 pip install pyinstaller"
            & $Python -m pip install pyinstaller
            if ($LASTEXITCODE -ne 0) { throw "安装 PyInstaller 失败（退出码 $LASTEXITCODE）" }
        }
    }
    $pyiVersion = (& $Python -c "import PyInstaller; print(PyInstaller.__version__)").Trim()
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 不可用，构建中止" }
    Write-Host "PyInstaller $pyiVersion"

    # ---- 2. 清理旧产物 ---------------------------------------------------
    # 这一步是尽力而为，不是构建的硬前提：PyInstaller 带 --noconfirm 时会自己
    # rmtree 输出目录。杀软扫描中、目录被资源管理器占用、或环境装了删除保护
    # 钩子时，Remove-Item 会抛错；那种情况下如实说明并交给 PyInstaller，
    # 比让整个构建在这里中止有用。
    Write-Step "清理旧产物"
    foreach ($stale in @($BundleDir, (Join-Path $RepoRoot 'build'))) {
        if (Test-Path -LiteralPath $stale) {
            try {
                Remove-Item -LiteralPath $stale -Recurse -Force -ErrorAction Stop
                Write-Host "已删除 $stale"
            } catch {
                Write-Host "未能删除 $stale（$($_.Exception.Message.Split([Environment]::NewLine)[0])），交由 PyInstaller --noconfirm 覆盖"
            }
        }
    }
    $specFile = Join-Path $RepoRoot 'PDFTool.spec'
    if (Test-Path -LiteralPath $specFile) {
        try {
            Remove-Item -LiteralPath $specFile -Force -ErrorAction Stop
        } catch {
            Write-Host "未能删除 $specFile，忽略"
        }
    }

    # ---- 3. 构建 ---------------------------------------------------------
    Write-Step "PyInstaller 构建（onedir）"
    $pyiArgs = @(
        '--noconfirm'
        '--clean'
        '--onedir'
        '--name', 'PDFTool'
        '--add-data', 'app/static;app/static'
        '--collect-all', 'pikepdf'
        '--collect-all', 'pymupdf'
        '--hidden-import', 'uvicorn.logging'
        '--hidden-import', 'uvicorn.loops.auto'
        '--hidden-import', 'uvicorn.loops.asyncio'
        '--hidden-import', 'uvicorn.protocols.http.auto'
        '--hidden-import', 'uvicorn.protocols.http.h11_impl'
        '--hidden-import', 'uvicorn.protocols.websockets.auto'
        '--hidden-import', 'uvicorn.lifespan.on'
        'run.py'
    )
    & $Python -m PyInstaller @pyiArgs
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败（退出码 $LASTEXITCODE）" }

    if (-not (Test-Path -LiteralPath $ExePath)) {
        throw "构建结束但找不到 $ExePath"
    }

    # 静态资源没打进去的话，程序能启动但首页 404 —— 在这里就拦下来。
    # PyInstaller 6.x 把数据文件放进 _internal\，5.x 直接放在 exe 旁边，两种都探。
    $bundledStatic = @(
        (Join-Path $BundleDir '_internal\app\static\index.html'),
        (Join-Path $BundleDir 'app\static\index.html')
    ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $bundledStatic) {
        throw "构建产物缺少静态资源 app\static\index.html（检查 --add-data 是否生效）"
    }
    Write-Host "静态资源已打包：$bundledStatic"

    # ---- 4. 第三方许可证 -------------------------------------------------
    Write-Step "生成 THIRD-PARTY-LICENSES.txt"
    # 用 sysconfig 的 purelib 而不是 site.getsitepackages()：后者在某些嵌入式
    # 发行版里返回的是 prefix 而不是 Lib\site-packages，扫出来一个 dist-info 都没有。
    $purelib = (& $Python -c "import sysconfig; print(sysconfig.get_paths()['purelib'])").Trim()
    $licenseDir = Join-Path $BundleDir 'licenses'
    if (-not (Test-Path -LiteralPath $licenseDir)) {
        New-Item -ItemType Directory -Path $licenseDir | Out-Null
    }
    $distInfos = @()
    if (Test-Path -LiteralPath $purelib) {
        $distInfos = @(Get-ChildItem -LiteralPath $purelib -Directory -Filter '*.dist-info' -ErrorAction SilentlyContinue)
    }

    $components = @(
        @{ Name = 'PyMuPDF';  Dist = 'pymupdf';  Version = '1.26.4';     License = 'AGPL-3.0';     Home = 'https://github.com/pymupdf/PyMuPDF' }
        @{ Name = 'pikepdf';  Dist = 'pikepdf';  Version = '10.17.0';    License = 'MPL-2.0';      Home = 'https://github.com/pikepdf/pikepdf' }
        @{ Name = 'qpdf';     Dist = '';         Version = '随 pikepdf'; License = 'Apache-2.0';   Home = 'https://github.com/qpdf/qpdf' }
        @{ Name = 'FastAPI';  Dist = 'fastapi';  Version = '0.162.0';    License = 'MIT';          Home = 'https://github.com/fastapi/fastapi' }
        @{ Name = 'uvicorn';  Dist = 'uvicorn';  Version = '0.54.0';     License = 'BSD-3-Clause'; Home = 'https://github.com/encode/uvicorn' }
        @{ Name = 'pydantic'; Dist = 'pydantic'; Version = '2.14.2';     License = 'MIT';          Home = 'https://github.com/pydantic/pydantic' }
    )

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add('PDF 自动设置工具 —— 第三方组件许可证清单')
    $lines.Add('')
    $lines.Add('本程序在本地处理 PDF 文件，使用了下列第三方组件。')
    $lines.Add('各组件的许可证原文见本文件末尾列出的 licenses\ 目录。')
    $lines.Add('')
    $lines.Add('组件            版本            许可证')
    $lines.Add('--------------  --------------  ----------------')
    foreach ($item in $components) {
        $lines.Add(('{0,-14}  {1,-14}  {2}' -f $item.Name, $item.Version, $item.License))
    }
    $lines.Add('')
    $lines.Add('注意：PyMuPDF 以 AGPL-3.0 授权。若要在不分发源码的前提下分发本程序的二进制，')
    $lines.Add('需自行评估 AGPL 义务（或向 Artifex 购买商业许可）。')
    $lines.Add('')
    $lines.Add('随附许可证文件：')

    $copied = 0
    $missing = New-Object System.Collections.Generic.List[string]
    foreach ($item in $components) {
        if ([string]::IsNullOrEmpty($item.Dist)) { continue }
        $matched = $distInfos | Where-Object {
            # pymupdf-1.26.4.dist-info -> pymupdf
            (($_.Name -split '-\d')[0]).ToLowerInvariant() -eq $item.Dist
        }
        $itemCopied = 0
        foreach ($distInfo in $matched) {
            $licenseFiles = @(Get-ChildItem -LiteralPath $distInfo.FullName -File -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -like 'LICENSE*' -or $_.Name -like 'COPYING*' -or $_.Name -like 'NOTICE*' })
            foreach ($file in $licenseFiles) {
                $target = Join-Path $licenseDir ("{0}-{1}" -f $item.Name, $file.Name)
                Copy-Item -LiteralPath $file.FullName -Destination $target -Force
                $lines.Add(('  licenses\{0}' -f (Split-Path -Leaf $target)))
                $copied++
                $itemCopied++
            }
        }
        if ($itemCopied -eq 0) {
            $missing.Add(('  {0,-14}  {1}：wheel 内未附许可证文件，需按 {2} 自行补齐' -f `
                $item.Name, $item.Version, $item.Home))
        }
    }
    if ($copied -eq 0) {
        $lines.Add("  （未在 $purelib 中找到 LICENSE/COPYING 文件）")
    }
    if ($missing.Count -gt 0) {
        $lines.Add('')
        $lines.Add('未随 wheel 提供许可证原文的组件：')
        foreach ($line in $missing) { $lines.Add($line) }
    }

    $licensePath = Join-Path $BundleDir 'THIRD-PARTY-LICENSES.txt'
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($licensePath, ($lines -join "`r`n") + "`r`n", $utf8NoBom)
    Write-Host "已写入 $licensePath（随附 $copied 个许可证文件）"

    # ---- 5. 汇总 ---------------------------------------------------------
    Write-Step "构建完成"
    $totalBytes = (Get-ChildItem -LiteralPath $BundleDir -Recurse -File | Measure-Object -Property Length -Sum).Sum
    Write-Host ("产物：{0}" -f $ExePath)
    Write-Host ("体积：{0:N1} MB" -f ($totalBytes / 1MB))
    Write-Host "下一步：powershell -ExecutionPolicy Bypass -File scripts\smoke_test.ps1"
    exit 0
}
catch {
    Write-Host ""
    Write-Host "构建失败：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
finally {
    Pop-Location
}
