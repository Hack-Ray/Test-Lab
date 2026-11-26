<#
.SYNOPSIS
  Test-Lab 一鍵部署：在一台 Windows 筆電上架好 VMware VM、Cloudflare Tunnel / Access、Portal。

.DESCRIPTION
  分四段，依序執行；每段都可以單獨重跑，已完成的部分會跳過：
    VMware      安裝 VMware Workstation Pro、確認 NAT 網段
    VMs         下載 Windows Server 2022 評估版、無人值守建立每台 VM（開好 RDP、固定 IP、測試帳號、閒置關機）
    Cloudflare  用 API 建 tunnel、私有網段路由、DNS、Access Group / Policy / 應用程式、瀏覽器 RDP
    Portal      安裝 Python 與 Portal、產生 portal.toml、登入時自動啟動

  需要人手的步驟（登入 Broadcom 下載 VMware、建立 Cloudflare API token、第一次開 Zero Trust）
  腳本會停下來提示。

.EXAMPLE
  # 以系統管理員身分開 PowerShell，在 repo 根目錄執行：
  powershell -ExecutionPolicy Bypass -File .\deploy\deploy.ps1

.EXAMPLE
  # 只重跑某一段
  powershell -ExecutionPolicy Bypass -File .\deploy\deploy.ps1 -Stage Cloudflare

.EXAMPLE
  # 先不接 Cloudflare，只在本機把 VM 和 Portal（dev 模式）架起來
  powershell -ExecutionPolicy Bypass -File .\deploy\deploy.ps1 -SkipCloudflare
#>
[CmdletBinding()]
param(
    [ValidateSet('All', 'VMware', 'VMs', 'Cloudflare', 'Portal')]
    [string]$Stage = 'All',
    [string]$ConfigPath,
    [switch]$SkipCloudflare
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $here

# ---- 系統管理員 ---------------------------------------------------------------
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal $id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host '需要系統管理員權限，正在重新以系統管理員身分啟動…' -ForegroundColor Yellow
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-NoExit', '-File', "`"$($MyInvocation.MyCommand.Path)`"", '-Stage', $Stage)
    if ($ConfigPath) { $argList += @('-ConfigPath', "`"$ConfigPath`"") }
    if ($SkipCloudflare) { $argList += '-SkipCloudflare' }
    Start-Process powershell.exe -Verb RunAs -ArgumentList $argList
    return
}

foreach ($m in 'Common', 'VMware', 'Cloudflare', 'Portal') {
    Import-Module (Join-Path $here "lib\$m.psm1") -Force -DisableNameChecking
}

# ---- 設定 ---------------------------------------------------------------------
if (-not $ConfigPath) { $ConfigPath = Join-Path $here 'lab.config.psd1' }
if (-not (Test-Path $ConfigPath)) {
    Copy-Item (Join-Path $here 'lab.config.example.psd1') $ConfigPath
    Write-Host "已建立設定檔 $ConfigPath（用預設值，要改網域或 VM 清單可以先中止去改）" -ForegroundColor Yellow
}
$Config = Import-PowerShellDataFile $ConfigPath
$logDir = Join-Path $Config.InstallRoot 'logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$deployLog = Join-Path $logDir ("deploy-{0:yyyyMMdd-HHmmss}.log" -f (Get-Date))
try { Start-Transcript -Path $deployLog | Out-Null } catch { }
Initialize-LabState (Join-Path $here '.state')
if (-not $Config.AdminEmail) {
    $saved = Get-LabStateValue 'AdminEmail'
    if ($saved) { $Config.AdminEmail = $saved }
    else {
        $Config.AdminEmail = (Read-Host '你的 email（會成為第一位 SA，也是第一個能通過 Cloudflare Access 的人）').Trim().ToLower()
        Set-LabState 'AdminEmail' $Config.AdminEmail
    }
}
$secretsDir = Join-Path $Config.InstallRoot 'secrets'
New-Item -ItemType Directory -Path $secretsDir -Force | Out-Null
Protect-LabPath $secretsDir
$tokenFile = Join-Path $secretsDir 'cf_api_token.txt'
$Secrets = Get-LabSecrets

function Should-Run([string]$Name) { return ($Stage -eq 'All' -or $Stage -eq $Name) }

Write-Host ''
Write-Host "Test-Lab 一鍵部署（$Stage）" -ForegroundColor Cyan
Write-Host "  Portal：https://$($Config.PortalHost)   SA：$($Config.AdminEmail)"

# ---- 1. VMware -----------------------------------------------------------------
Write-Step '1/4 VMware Workstation Pro'
$VMware = $null
if ((Should-Run 'VMware') -or (Should-Run 'VMs') -or (Should-Run 'Portal')) {
    if (Should-Run 'VMware') { $VMware = Install-LabVMware } else { $VMware = Find-LabVMware }
    if (-not $VMware) { throw '找不到 VMware Workstation，請先跑 -Stage VMware' }
}
$Net = $null
if ($VMware) {
    $Net = Get-LabNatNetwork
    Set-LabState 'Net' $Net
    Write-Ok "VM 網段：$($Net.Cidr)（閘道 $($Net.Gateway)）"
} else { $Net = Get-LabStateValue 'Net' }

# ---- 2. VMs --------------------------------------------------------------------
Write-Step '2/4 建立 Windows VM'
$Vms = @()
if (Should-Run 'VMs') {
    Get-LabWindowsIso $Config.IsoUrl $Config.IsoPath
    foreach ($vm in $Config.VMs) {
        $Vms += New-LabVm -Vm $vm -VMware $VMware -Net $Net -Config $Config -Secrets $Secrets -RepoRoot $repoRoot
    }
} else {
    foreach ($vm in $Config.VMs) {
        $s = Get-LabStateValue "vm_$($vm.Name)"
        if ($s) { $Vms += $s } else { Write-Note "$($vm.Name) 還沒建立（跑 -Stage VMs）" }
    }
    Write-Info '跳過'
}

# 健康檢查的 port 以設定檔為準（VM 建好後也能改，例如 AP / SQL 還沒裝時先只檢查 3389）
foreach ($v in $Vms) {
    $c = $Config.VMs | Where-Object { $_.Name -eq $v.Name } | Select-Object -First 1
    if ($c) { $v.Ports = @($c.Ports) }
}

# ---- 3. Cloudflare ---------------------------------------------------------------
Write-Step '3/4 Cloudflare Tunnel 與 Access'
$Cf = Get-LabStateValue 'Cloudflare'
if ($SkipCloudflare) {
    Write-Info '跳過（-SkipCloudflare），Portal 會用 dev 模式'
    $Cf = $null
} elseif (Should-Run 'Cloudflare') {
    if (-not $Net) { throw '還不知道 VM 網段，請先跑 -Stage VMware' }
    $Cf = Invoke-LabCloudflareSetup -Config $Config -Vms $Vms -Net $Net -TokenFile $tokenFile
    Set-LabState 'Cloudflare' $Cf
} else { Write-Info '跳過' }

# ---- 4. Portal ---------------------------------------------------------------------
Write-Step '4/4 Portal'
if (Should-Run 'Portal') {
    Install-LabPortal -Config $Config -Vms $Vms -Cf $Cf -RepoRoot $repoRoot -InstallRoot $Config.InstallRoot `
        -VmrunPath $VMware.Vmrun -TokenFile $tokenFile
} else { Write-Info '跳過' }

# ---- 完成 ---------------------------------------------------------------------------
$secretsFile = Join-Path $here '.state\secrets.json'
Write-Host ''
Write-Host '============================================================' -ForegroundColor Green
Write-Host ' 部署完成' -ForegroundColor Green
Write-Host '============================================================' -ForegroundColor Green
if ($Cf) {
    Write-Host "  Portal：         https://$($Config.PortalHost)（用 $($Config.AdminEmail) 登入）"
    Write-Host "  App Launcher：   https://$($Cf.TeamDomain)"
} else {
    Write-Host '  Portal（本機）： http://127.0.0.1:8080'
}
foreach ($v in $Vms) { Write-Host ("  VM：{0,-12} {1,-7} {2}" -f $v.Name, $v.Kind, $v.Ip) }
Write-Host "  Windows 帳號：   $($Config.TesterUser)（共用測試帳號）、Administrator"
Write-Host "  密碼在：         $secretsFile（只有你能讀，不要放進 git 或傳出去）"
Write-Host ''
Write-Host '  之後：'
Write-Host '   - 新增使用者：Portal → 使用者管理（會自動同步 Cloudflare Access Group）'
Write-Host '   - 要讓 Portal 在開機後自動跑，這台筆電要登入你的 Windows 帳號（可以設自動登入）'
Write-Host '   - 改了 lab.config.psd1（例如加 VM）就重跑 deploy.ps1'
Write-Host "  這次部署的紀錄：$deployLog"
try { Stop-Transcript | Out-Null } catch { }
