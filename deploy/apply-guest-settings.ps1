<#
.SYNOPSIS
  在主機上對已建好的 VM 套用：台北時區、繁體中文注音輸入、系統地區 zh-TW、關閉 Shutdown Event Tracker。
  不用進 VM 打字（透過 VMware Tools 的 vmrun runProgramInGuest），套用完會重開 VM。

.EXAMPLE
  # 以系統管理員身分在 repo 根目錄執行，VM 要是開機狀態
  powershell -ExecutionPolicy Bypass -File .\deploy\apply-guest-settings.ps1
  powershell -ExecutionPolicy Bypass -File .\deploy\apply-guest-settings.ps1 -Name cli-01
#>
[CmdletBinding()]
param(
    [string[]]$Name,
    [string]$ConfigPath,
    [switch]$NoRestart
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
foreach ($m in 'Common', 'VMware') { Import-Module (Join-Path $here "lib\$m.psm1") -Force -DisableNameChecking }
if (-not $ConfigPath) { $ConfigPath = Join-Path $here 'lab.config.psd1' }
$Config = Import-PowerShellDataFile $ConfigPath
Initialize-LabState (Join-Path $here '.state')
$Secrets = Get-LabSecrets
$VMware = Find-LabVMware
if (-not $VMware) { throw '找不到 VMware Workstation' }

$ps = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'

function Invoke-InGuest([string]$Vmx, [string]$User, [string]$Password, [string]$Script) {
    $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Script))
    & $VMware.Vmrun -T ws -gu $User -gp $Password runProgramInGuest $Vmx $ps "-NoProfile -ExecutionPolicy Bypass -EncodedCommand $enc" 2>&1 | Out-Null
    return $LASTEXITCODE
}

$adminScript = @'
Set-TimeZone -Id 'Taipei Standard Time'
$rel = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows NT\Reliability'
New-Item -Path $rel -Force | Out-Null
Set-ItemProperty $rel -Name ShutdownReasonOn -Value 0 -Type DWord
Set-ItemProperty $rel -Name ShutdownReasonUI -Value 0 -Type DWord
try { if (Get-Command Install-Language -ErrorAction SilentlyContinue) { Install-Language -Language zh-TW | Out-Null } } catch { }
$l = New-WinUserLanguageList 'en-US'; $l.Add('zh-TW'); Set-WinUserLanguageList $l -Force
Set-WinSystemLocale zh-TW
Set-Culture zh-TW
if (Get-Command Copy-UserInternationalSettingsToSystem -ErrorAction SilentlyContinue) {
    Copy-UserInternationalSettingsToSystem -WelcomeScreen $true -NewUser $true
}
'@
$userScript = @'
$l = New-WinUserLanguageList 'en-US'; $l.Add('zh-TW'); Set-WinUserLanguageList $l -Force
Set-Culture zh-TW
'@

foreach ($vm in $Config.VMs) {
    if ($Name -and ($Name -notcontains $vm.Name)) { continue }
    $s = Get-LabStateValue "vm_$($vm.Name)"
    if (-not $s) { Write-Note "$($vm.Name) 還沒建立，跳過"; continue }
    Write-Step "$($vm.Name)"
    if (-not (Test-LabVmRunning $VMware $s.Vmx)) { Write-Note '沒開機，跳過（先從 Portal 或 VMware 開機）'; continue }
    Write-Info '系統設定（Install-Language 要下載語言套件，可能要幾分鐘）…'
    if ((Invoke-InGuest $s.Vmx 'Administrator' $Secrets.AdminPassword $adminScript) -ne 0) {
        Write-Note '執行失敗：VM 裡可能沒有 VMware Tools，或 Administrator 密碼不對。可以改照手冊手動設定'
        continue
    }
    Write-Ok '時區、中文、關機原因視窗已設定'
    if ((Invoke-InGuest $s.Vmx $Config.TesterUser $Secrets.TesterPassword $userScript) -eq 0) { Write-Ok "$($Config.TesterUser) 已加上注音" }
    else { Write-Note "$($Config.TesterUser) 的輸入法沒設成功（帳號可能還沒登入過），之後在 VM 裡 Settings → Language 加中文(台灣)" }
    if (-not $NoRestart) {
        & $VMware.Vmrun -T ws -gu Administrator -gp $Secrets.AdminPassword runProgramInGuest $s.Vmx -noWait 'C:\Windows\System32\shutdown.exe' '/r /t 5 /d p:0:0' 2>&1 | Out-Null
        Write-Ok '重開機中（約 1–2 分鐘）'
    }
}
