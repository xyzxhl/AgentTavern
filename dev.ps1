<#
.SYNOPSIS
    一条命令把前后端一起起起来。

.DESCRIPTION
    后端 uvicorn 跑 8000，前端 vite 跑 5173，**两个都挂在当前控制台上**，
    日志交错打在一起，Ctrl+C 一次全停。

    端口没做成参数，是故意的。`vite.config.ts` 里代理目标写死 `127.0.0.1:8000`、
    监听写死 `127.0.0.1:5173`；这里再开一个 `-Port` 只会让两边悄悄对不上——
    前端照样起得来，请求却被 Vite 当成前端路由回一份 index.html **且状态码 200**
    （S135 那个坑）。要换端口就去改 vite.config.ts，一处。

.PARAMETER Setup
    先装依赖再起。新 clone 下来第一次用这个——仓库里没有 .venv 和 node_modules。

.PARAMETER NoReload
    关掉 uvicorn 的自动重载。改后端代码不想让它反复重启时用。

.EXAMPLE
    .\dev.ps1
.EXAMPLE
    .\dev.ps1 -Setup
#>
[CmdletBinding()]
param(
    [switch]$Setup,
    [switch]$NoReload
)

$ErrorActionPreference = 'Stop'

$Root     = $PSScriptRoot
$Backend  = Join-Path $Root 'backend'
$Frontend = Join-Path $Root 'frontend'
$Python   = Join-Path $Backend '.venv\Scripts\python.exe'

function Fail([string]$what, [string]$fix) {
    Write-Host ''
    Write-Host "  $what" -ForegroundColor Red
    Write-Host "  → $fix" -ForegroundColor Yellow
    Write-Host ''
    exit 1
}

# -- 装依赖 ------------------------------------------------------------------

if ($Setup) {
    if (-not (Test-Path $Python)) {
        Write-Host '建虚拟环境 backend\.venv ...' -ForegroundColor Cyan
        & py -3 -m venv (Join-Path $Backend '.venv')
        if ($LASTEXITCODE -ne 0) { Fail '建虚拟环境失败。' '确认装了 Python 3.11+（py -3 --version）。' }
    }
    Write-Host '装后端依赖 ...' -ForegroundColor Cyan
    & $Python -m pip install -q -e "$Backend[dev]"
    if ($LASTEXITCODE -ne 0) { Fail '后端依赖装不上。' '看上面 pip 的报错。' }

    Write-Host '装前端依赖 ...' -ForegroundColor Cyan
    Push-Location $Frontend
    try {
        & npm install
        if ($LASTEXITCODE -ne 0) { Fail '前端依赖装不上。' '看上面 npm 的报错。' }
    } finally { Pop-Location }
    Write-Host ''
}

# -- 起之前先把话说清楚 ------------------------------------------------------
#
# 这几条全是「起不来的时候，报错离原因很远」的：uvicorn 找不到 .env 不会报错，
# 它会拿一份空 key 起得好好的，直到第一次真调模型才 401。宁可在这里拦住。

if (-not (Test-Path $Python)) {
    Fail "没有虚拟环境：$Python" '跑 .\dev.ps1 -Setup'
}
if (-not (Test-Path (Join-Path $Frontend 'node_modules'))) {
    Fail '前端依赖没装（缺 frontend\node_modules）。' '跑 .\dev.ps1 -Setup'
}
if (-not (Test-Path (Join-Path $Backend '.env'))) {
    Fail '缺 backend\.env —— 服务能起来，但一调模型就 401。' `
         'copy backend\.env.example backend\.env 然后填 LLM_API_KEY'
}

# 模型名按 agent 各设一份（S159），住在那几份 YAML 顶上。**这道检查因此从
# 「.env 里有没有 LLM_MODEL」挪到了这里**——空的那份会在第一次真调模型时抛，
# 而那时已经是一轮跑到一半了。
$blank = Get-ChildItem (Join-Path $Backend 'agenttavern\prompting\*.yaml') | Where-Object {
    -not ((Get-Content $_.FullName -Encoding utf8) -match '^model:\s*\S')
}
if ($blank) {
    Fail "这几份提示词顶上没写 model：$(($blank.Name) -join ', ')" `
         '打开那几份 YAML，在顶上写 model: <模型名>'
}

# 端口被占：uvicorn 自己的报错是一行 WinError 10048，看不出是谁占的。
foreach ($p in @(@{Port = 8000; Who = '后端' }, @{Port = 5173; Who = '前端' })) {
    $held = Get-NetTCPConnection -State Listen -LocalPort $p.Port -ErrorAction SilentlyContinue
    if ($held) {
        $pids = @($held.OwningProcess | Sort-Object -Unique)
        $names = ($pids | ForEach-Object {
                $proc = Get-Process -Id $_ -ErrorAction SilentlyContinue
                if ($proc) { "$($proc.ProcessName)($_)" } else { "pid $_" }
            }) -join ', '
        Fail "$($p.Who)端口 $($p.Port) 已被占用：$names" `
             "多半是上次没退干净。taskkill /PID $($pids[0]) /T /F"
    }
}

$npm = (Get-Command npm.cmd -ErrorAction SilentlyContinue)
if (-not $npm) { Fail '找不到 npm。' '装 Node.js，或确认它在 PATH 里。' }

# -- 起 ----------------------------------------------------------------------

# **必须在 backend\ 里起**：`.env` 和 `DATA_DIR=data` 都是相对 CWD 解析的
# （见 config.py）。在仓库根起，它会安静地读不到 .env、把数据写到根目录下。
# 不叫 $args —— 那是 PowerShell 的自动变量，脚本作用域里写它会踩到自己。
$serve = @(
    '-m', 'uvicorn',
    'agenttavern.server.app:create_app', '--factory',   # app 是工厂，不是模块级对象
    '--host', '127.0.0.1', '--port', '8000'
)
if (-not $NoReload) { $serve += '--reload' }

$running = @()

try {
    Write-Host '后端  http://127.0.0.1:8000' -ForegroundColor DarkGray
    $be = Start-Process -FilePath $Python -ArgumentList $serve `
        -WorkingDirectory $Backend -NoNewWindow -PassThru
    # **摸一下 .Handle**：`Start-Process -PassThru` 不替你留句柄，进程一没
    # `.ExitCode` 就再也读不到了，日志里只会剩一句「exit 」。碰一下就缓存住。
    $null = $be.Handle
    $running += @{ Proc = $be; Name = '后端' }

    Write-Host '前端  http://127.0.0.1:5173  ← 打这个' -ForegroundColor Green
    Write-Host 'Ctrl+C 停止两个。' -ForegroundColor DarkGray
    Write-Host ''
    $fe = Start-Process -FilePath $npm.Source -ArgumentList @('run', 'dev') `
        -WorkingDirectory $Frontend -NoNewWindow -PassThru
    $null = $fe.Handle
    $running += @{ Proc = $fe; Name = '前端' }

    # 一个挂了就把另一个也停掉。否则前端还在，代理却指着一个不存在的后端，
    # 症状是页面正常打开、点什么都没反应——比两个一起挂难查得多。
    while ($true) {
        foreach ($r in $running) {
            if ($r.Proc.HasExited) {
                $code = $r.Proc.ExitCode
                if ($null -eq $code) { $code = '被强制终止' }
                Write-Host ''
                Write-Host "$($r.Name)退出了（exit $code），另一个也停掉。" -ForegroundColor Yellow
                return
            }
        }
        Start-Sleep -Milliseconds 400
    }
}
finally {
    # **按进程树杀**。uvicorn --reload 是父进程守着一个真正干活的子进程，
    # 只杀父进程会留下一个孤儿继续占着 8000，下次再起就撞端口。
    foreach ($r in $running) {
        if (-not $r.Proc.HasExited) {
            & taskkill.exe /PID $r.Proc.Id /T /F 2>&1 | Out-Null
        }
    }
}
