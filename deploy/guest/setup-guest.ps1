<#
.SYNOPSIS
  Windows 安裝完第一次登入時執行（由 autounattend.xml 的 FirstLogonCommands 呼叫）。
  設定固定 IP、開 RDP、建測試帳號、裝 VMware Tools、註冊閒置關機排程，最後重開機。
  設定值從同一張光碟上的 guest-config.json 讀取。
#>
$ErrorActionPreference = 'Continue'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$cfg = Get-Content (Join-Path $here 'guest-config.json') -Raw | ConvertFrom-Json
$root = 'C:\TestLab'
New-Item -ItemType Directory -Path $root -Force | Out-Null
Start-Transcript -Path (Join-Path $root 'setup-guest.log') -Append | Out-Null

# ---- 固定 IP ---------------------------------------------------------------
$nic = Get-NetAdapter | Where-Object Status -eq 'Up' | Sort-Object ifIndex | Select-Object -First 1
if ($nic) {
    Get-NetIPAddress -InterfaceIndex $nic.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
    Get-NetRoute -InterfaceIndex $nic.ifIndex -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
        Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
    New-NetIPAddress -InterfaceIndex $nic.ifIndex -IPAddress $cfg.Ip -PrefixLength $cfg.PrefixLength `
        -DefaultGateway $cfg.Gateway | Out-Null
    Set-DnsClientServerAddress -InterfaceIndex $nic.ifIndex -ServerAddresses $cfg.Dns
    # 網路設為私人，避免防火牆用公用設定檔擋掉
    Get-NetConnectionProfile -InterfaceIndex $nic.ifIndex -ErrorAction SilentlyContinue |
        Set-NetConnectionProfile -NetworkCategory Private -ErrorAction SilentlyContinue
}

# ---- 開 RDP（瀏覽器 RDP 需要 TLS：安全層用 Negotiate） ---------------------
Set-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server' -Name fDenyTSConnections -Value 0
$rdpTcp = 'HKLM:\System\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp'
Set-ItemProperty $rdpTcp -Name SecurityLayer -Value 1
# 斷線的工作階段 8 小時後登出，避免一直佔著
$ts = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services'
New-Item -Path $ts -Force | Out-Null
Set-ItemProperty $ts -Name MaxDisconnectionTime -Value (8 * 3600 * 1000)

# ---- 關掉 Shutdown Event Tracker（測試機常被直接關電源，不要每次登入都問關機原因） ----
$rel = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows NT\Reliability'
New-Item -Path $rel -Force | Out-Null
Set-ItemProperty $rel -Name ShutdownReasonOn -Value 0 -Type DWord
Set-ItemProperty $rel -Name ShutdownReasonUI -Value 0 -Type DWord

# ---- 其他要開的 port（server 的 AP / SQL） -----------------------------------
$extra = @($cfg.Ports | Where-Object { $_ -ne 3389 })
if ($extra.Count -gt 0) {
    New-NetFirewallRule -DisplayName 'Test-Lab app ports' -Direction Inbound -Protocol TCP `
        -LocalPort $extra -Action Allow -Profile Any -ErrorAction SilentlyContinue | Out-Null
}
# Portal 在 host 上用 ping 以外的方式檢查，順便允許 ping 方便排查
Enable-NetFirewallRule -Name 'FPS-ICMP4-ERQ-In' -ErrorAction SilentlyContinue

# ---- 固定測試帳號 -------------------------------------------------------------
$pw = ConvertTo-SecureString $cfg.TesterPassword -AsPlainText -Force
if (-not (Get-LocalUser -Name $cfg.TesterUser -ErrorAction SilentlyContinue)) {
    New-LocalUser -Name $cfg.TesterUser -Password $pw -PasswordNeverExpires -AccountNeverExpires `
        -Description 'Test-Lab 共用測試帳號' | Out-Null
}
Add-LocalGroupMember -Group 'Remote Desktop Users' -Member $cfg.TesterUser -ErrorAction SilentlyContinue
if ($cfg.TesterIsAdmin) {
    Add-LocalGroupMember -Group 'Administrators' -Member $cfg.TesterUser -ErrorAction SilentlyContinue
}

# ---- 小調整 ---------------------------------------------------------------------
Get-ScheduledTask -TaskName ServerManager -ErrorAction SilentlyContinue | Disable-ScheduledTask | Out-Null
powercfg.exe /change monitor-timeout-ac 0
powercfg.exe /change standby-timeout-ac 0

# ---- 閒置關機 ---------------------------------------------------------------------
Copy-Item (Join-Path $here 'idle-shutdown.ps1') (Join-Path $root 'idle-shutdown.ps1') -Force
$portsArg = ''
if ($cfg.WatchPorts -and @($cfg.WatchPorts).Count -gt 0) { $portsArg = ' -WatchPorts ' + (@($cfg.WatchPorts) -join ',') }
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ("-NoProfile -ExecutionPolicy Bypass -File `"$root\idle-shutdown.ps1`" -IdleMinutes {0}{1}" -f $cfg.IdleMinutes, $portsArg)
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName 'TestLab-IdleShutdown' -Action $action -Trigger $trigger `
    -User 'SYSTEM' -RunLevel Highest -Force | Out-Null

# ---- 關掉自動登入（autounattend 只為了跑這支腳本才開） -----------------------
$winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
Set-ItemProperty $winlogon -Name AutoAdminLogon -Value '0'
Remove-ItemProperty $winlogon -Name DefaultPassword -ErrorAction SilentlyContinue

# ---- VMware Tools（最後裝，裝完會要求重開機） ----------------------------------
$tools = Get-Volume | Where-Object { $_.DriveType -eq 'CD-ROM' -and $_.DriveLetter } |
    ForEach-Object { "$($_.DriveLetter):\setup64.exe" } | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($tools) {
    Start-Process -FilePath $tools -ArgumentList '/S /v"/qn REBOOT=R"' -Wait
}

# 最後才開防火牆的 RDP：host 端用 3389 能不能連來判斷安裝是否完成
Enable-NetFirewallRule -DisplayGroup 'Remote Desktop' -ErrorAction SilentlyContinue
Set-Content -Path (Join-Path $root 'setup-done.txt') -Value (Get-Date -Format o)
Stop-Transcript | Out-Null
Restart-Computer -Force
