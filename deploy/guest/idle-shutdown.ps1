<#
.SYNOPSIS
  VM 內的閒置關機檢查。由工作排程器每 5 分鐘以 SYSTEM 執行一次。

  「忙碌」的定義（任一成立就算有人在用）：
    - quser 有 Active 的工作階段，且閒置時間 < IdleMinutes
    - WatchPorts（例如 server 的 AP / SQL port）有從別台機器連進來的連線
  連續 IdleMinutes 分鐘都不忙碌，就關機。剛開機的前 IdleMinutes 分鐘不會關。
  Portal 會在 VM 關機後自動把分配標成結束。
#>
param(
    [int]$IdleMinutes = 30,
    [int[]]$WatchPorts = @(),
    [string]$StatePath = 'C:\TestLab\idle-state.json',
    [switch]$DryRun
)

function ConvertTo-IdleMinutes([string]$Text) {
    <# quser 的 IDLE TIME：'.'、'none'、'5'、'1:05'、'2+03:04'（天+時:分） #>
    $t = $Text.Trim()
    if ($t -eq '.' -or $t -eq 'none' -or $t -eq '') { return 0 }
    $days = 0
    if ($t -match '^(\d+)\+(.+)$') { $days = [int]$Matches[1]; $t = $Matches[2] }
    if ($t -match '^(\d+):(\d+)$') { return $days * 1440 + [int]$Matches[1] * 60 + [int]$Matches[2] }
    if ($t -match '^\d+$') { return $days * 1440 + [int]$t }
    return 0
}

function ConvertFrom-Quser([string[]]$Lines) {
    $sessions = @()
    foreach ($line in $Lines) {
        if ([string]::IsNullOrWhiteSpace($line) -or $line -match '^\s*USERNAME\s') { continue }
        # 有些工作階段沒有 SESSIONNAME（例如已中斷的），所以那一欄是選擇性的
        $m = [regex]::Match($line, '^.(?<user>\S+)\s+(?:(?<session>\S+)\s+)?(?<id>\d+)\s+(?<state>\S+)\s+(?<idle>\S+)\s+(?<logon>.+)$')
        if (-not $m.Success) { continue }
        $sessions += [pscustomobject]@{
            User        = $m.Groups['user'].Value
            Session     = $m.Groups['session'].Value
            Id          = [int]$m.Groups['id'].Value
            State       = $m.Groups['state'].Value
            IdleMinutes = ConvertTo-IdleMinutes $m.Groups['idle'].Value
        }
    }
    return , $sessions
}

function Test-SessionsBusy($Sessions, [int]$IdleMinutes) {
    foreach ($s in $Sessions) {
        if ($s.State -eq 'Active' -and $s.IdleMinutes -lt $IdleMinutes) { return $true }
    }
    return $false
}

function Test-PortsBusy([int[]]$Ports) {
    if (-not $Ports -or $Ports.Count -eq 0) { return $false }
    $conns = Get-NetTCPConnection -State Established -ErrorAction SilentlyContinue |
        Where-Object { $Ports -contains $_.LocalPort -and $_.RemoteAddress -notin @('127.0.0.1', '::1') }
    return [bool]$conns
}

function Get-IdleDecision([datetime]$Now, [datetime]$LastBusy, [datetime]$BootTime, [bool]$Busy, [int]$IdleMinutes) {
    if ($Busy) { return [pscustomobject]@{ LastBusy = $Now; Shutdown = $false } }
    $last = $LastBusy
    if ($BootTime -gt $last) { $last = $BootTime }
    $idle = ($Now - $last).TotalMinutes
    return [pscustomobject]@{ LastBusy = $last; Shutdown = ($idle -ge $IdleMinutes) }
}

function Invoke-IdleCheck {
    $now = Get-Date
    $boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
    $raw = & quser.exe 2>$null   # 沒有任何工作階段時 quser 回傳非 0，輸出是空的
    $sessions = ConvertFrom-Quser @($raw)
    $busy = (Test-SessionsBusy $sessions $IdleMinutes) -or (Test-PortsBusy $WatchPorts)

    $last = $boot
    if (Test-Path $StatePath) {
        try { $last = [datetime](Get-Content $StatePath -Raw | ConvertFrom-Json).LastBusy } catch { }
    }
    $d = Get-IdleDecision $now $last $boot $busy $IdleMinutes
    $dir = Split-Path $StatePath
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    @{ LastBusy = $d.LastBusy.ToString('o'); CheckedAt = $now.ToString('o'); Busy = $busy } |
        ConvertTo-Json | Set-Content -Path $StatePath -Encoding UTF8

    if ($d.Shutdown) {
        if ($DryRun) { Write-Output "會關機（已閒置 $IdleMinutes 分鐘以上）"; return }
        & shutdown.exe /s /t 60 /c "Test-Lab：閒置超過 $IdleMinutes 分鐘，自動關機" /d p:0:0
    }
}

# 被 dot-source（測試）時不執行
if ($MyInvocation.InvocationName -ne '.') { Invoke-IdleCheck }
