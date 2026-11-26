<#
.SYNOPSIS
  在主機上改已建好 VM 的 Administrator 或 tester 密碼（不用進 VM），並更新 deploy\.state\secrets.json。
  透過 VMware Tools 的 vmrun runProgramInGuest，VM 要是開機狀態。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\deploy\set-vm-password.ps1 -User tester
  powershell -ExecutionPolicy Bypass -File .\deploy\set-vm-password.ps1 -User Administrator
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('Administrator', 'tester')][string]$User,
    [string[]]$Name,
    [string]$ConfigPath
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
foreach ($m in 'Common', 'VMware') { Import-Module (Join-Path $here "lib\$m.psm1") -Force -DisableNameChecking }
if (-not $ConfigPath) { $ConfigPath = Join-Path $here 'lab.config.psd1' }
$Config = Import-PowerShellDataFile $ConfigPath
Initialize-LabState (Join-Path $here '.state')
$Secrets = Get-LabSecrets $Config
$VMware = Find-LabVMware
if (-not $VMware) { throw '找不到 VMware Workstation' }
$account = if ($User -eq 'tester') { $Config.TesterUser } else { 'Administrator' }

# 用打的（PowerShell 5.1 的隱藏輸入不能貼上，但打字沒問題）
while ($true) {
    $p1 = Read-Host "新的 $account 密碼（大小寫、數字、符號，至少 8 碼）" -AsSecureString
    $p2 = Read-Host '再輸入一次' -AsSecureString
    $a = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($p1))
    $b = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($p2))
    if ($a -ne $b) { Write-Note '兩次不一樣，請重打'; continue }
    if ($a.Length -lt 8) { Write-Note '至少 8 碼'; continue }
    break
}

# 用 Administrator 的身分在 VM 裡改密碼；密碼用 base64 帶進去，避免引號、符號被吃掉
$pwB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($a))
$script = @"
`$pw = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$pwB64'))
`$sec = ConvertTo-SecureString `$pw -AsPlainText -Force
Set-LocalUser -Name '$account' -Password `$sec
"@

$okCount = 0; $failed = @()
foreach ($vm in $Config.VMs) {
    if ($Name -and ($Name -notcontains $vm.Name)) { continue }
    $s = Get-LabStateValue "vm_$($vm.Name)"
    if (-not $s) { continue }
    if (-not (Test-LabVmRunning $VMware $s.Vmx)) { Write-Note "$($vm.Name) 沒開機，跳過"; $failed += $vm.Name; continue }
    Write-Info "$($vm.Name)：檢查 VMware Tools…"
    $tools = Get-LabToolsState $VMware $s.Vmx
    if ($tools -ne 'running') { Write-Note "$($vm.Name)：VMware Tools 狀態是 $tools（要 running 才能從主機操作），跳過"; $failed += $vm.Name; continue }
    Write-Info "$($vm.Name)：改密碼中…"
    if (Invoke-LabGuestPs $VMware $s.Vmx 'Administrator' $Secrets.AdminPassword $script 120) { Write-Ok "$($vm.Name)：$account 密碼已更新"; $okCount++ }
    else { Write-Note "$($vm.Name)：失敗（VM 裡沒有 VMware Tools，或目前記錄的 Administrator 密碼不對）"; $failed += $vm.Name }
}

if ($okCount -gt 0) {
    if ($User -eq 'tester') { $Secrets.TesterPassword = $a } else { $Secrets.AdminPassword = $a }
    Save-LabSecrets $Secrets
    Write-Ok '已更新 deploy\.state\secrets.json'
}
if ($failed.Count -gt 0) {
    Write-Note ("這幾台沒改到：{0}。它們還是舊密碼，開機後用 -Name 單獨再跑一次" -f ($failed -join '、'))
    if ($okCount -gt 0 -and $User -eq 'Administrator') { Write-Note '注意：secrets.json 已是新密碼，舊密碼請先記下來，重跑時腳本會用新密碼登入而失敗' }
}
