# 共用小工具：輸出、等待使用者、狀態檔、密碼、TCP 檢查。
# 相容 Windows PowerShell 5.1。

$script:StateDir = $null

function Initialize-LabState([string]$Dir) {
    $script:StateDir = $Dir
    if (-not (Test-Path $Dir)) { New-Item -ItemType Directory -Path $Dir -Force | Out-Null }
    Protect-LabPath $Dir
}

function Write-Step([string]$Text) { Write-Host ''; Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text)   { Write-Host "    [OK] $Text" -ForegroundColor Green }
function Write-Info([string]$Text) { Write-Host "    $Text" }
function Write-Note([string]$Text) { Write-Host "    [注意] $Text" -ForegroundColor Yellow }

function Wait-LabUser {
    <# 需要人手的步驟：印出說明，等使用者按 Enter。 #>
    param([string]$Title, [string[]]$Steps)
    Write-Host ''
    Write-Host "------------------------------------------------------------" -ForegroundColor Yellow
    Write-Host " 需要你手動處理：$Title" -ForegroundColor Yellow
    Write-Host "------------------------------------------------------------" -ForegroundColor Yellow
    $i = 1
    foreach ($s in $Steps) { Write-Host ("  {0}. {1}" -f $i, $s); $i++ }
    Write-Host ''
    [void](Read-Host '完成後按 Enter 繼續（Ctrl+C 可中止，之後重跑會從這一步接著做）')
}

function Read-LabFromClipboard([string]$What) {
    <#
      Windows PowerShell 5.1 的隱藏輸入（Read-Host -AsSecureString）不能整段貼上，
      所以改成：使用者先複製，按 Enter 後從剪貼簿讀取。讀完清掉剪貼簿，不在畫面上顯示。
    #>
    while ($true) {
        [void](Read-Host "請先複製 $What，回到這個視窗按 Enter（腳本會從剪貼簿讀取，不會顯示在畫面上）")
        $v = (Get-Clipboard | Out-String).Trim().Trim("'", '"')
        if ($v -and $v -notmatch '\s') {
            try { Set-Clipboard -Value ' ' } catch { }
            return $v
        }
        Write-Note "剪貼簿裡不是 $What（空的或含空白），請再複製一次"
    }
}

function Read-LabSecret([string]$Prompt) {
    $sec = Read-Host -Prompt $Prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

# ---- 狀態檔（讓每一段都能重跑） -------------------------------------------
function Get-LabState {
    $path = Join-Path $script:StateDir 'state.json'
    if (Test-Path $path) { return (Get-Content $path -Raw -Encoding UTF8 | ConvertFrom-Json) }
    return [pscustomobject]@{}
}

function Set-LabState([string]$Key, $Value) {
    $state = Get-LabState
    $state | Add-Member -NotePropertyName $Key -NotePropertyValue $Value -Force
    $path = Join-Path $script:StateDir 'state.json'
    $state | ConvertTo-Json -Depth 20 | Set-Content -Path $path -Encoding UTF8
}

function Get-LabStateValue([string]$Key) {
    $state = Get-LabState
    if ($state.PSObject.Properties.Name -contains $Key) { return $state.$Key }
    return $null
}

# ---- 密碼與檔案權限 ---------------------------------------------------------
function New-LabPassword([int]$Length = 16) {
    # Windows 複雜度：大小寫、數字、符號都要有；避開容易看錯的字元
    $upper = 'ABCDEFGHJKLMNPQRSTUVWXYZ'; $lower = 'abcdefghijkmnpqrstuvwxyz'
    $digit = '23456789'; $sym = '!@#%*-_=+'
    $all = $upper + $lower + $digit + $sym
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $bytes = New-Object byte[] ($Length)
    $rng.GetBytes($bytes)
    $chars = @(
        $upper[$bytes[0] % $upper.Length], $lower[$bytes[1] % $lower.Length],
        $digit[$bytes[2] % $digit.Length], $sym[$bytes[3] % $sym.Length]
    )
    for ($i = 4; $i -lt $Length; $i++) { $chars += $all[$bytes[$i] % $all.Length] }
    return -join ($chars | Sort-Object { Get-Random })
}

function Protect-LabPath([string]$Path) {
    <# 只讓目前使用者、Administrators、SYSTEM 讀取（放密碼和 token 的地方）。 #>
    if (-not $IsWindows -and $PSVersionTable.PSEdition -eq 'Core') { return }
    try {
        & icacls.exe $Path /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" | Out-Null
    } catch { Write-Note "無法設定 $Path 的權限：$_" }
}

function Get-LabSecrets($Config) {
    # 第一次產生：設定檔有填 AdminPassword / TesterPassword 就用，沒填就隨機
    # 之後以 secrets.json 為準（VM 已用這組密碼建好）；要改已建好 VM 的密碼用 set-vm-password.ps1
    $path = Join-Path $script:StateDir 'secrets.json'
    if (Test-Path $path) { return (Get-Content $path -Raw -Encoding UTF8 | ConvertFrom-Json) }
    $admin = if ($Config -and $Config.AdminPassword) { $Config.AdminPassword } else { New-LabPassword }
    $tester = if ($Config -and $Config.TesterPassword) { $Config.TesterPassword } else { New-LabPassword }
    $s = [pscustomobject]@{ AdminPassword = $admin; TesterPassword = $tester }
    Save-LabSecrets $s
    return $s
}

function Save-LabSecrets($Secrets) {
    $Secrets | ConvertTo-Json | Set-Content -Path (Join-Path $script:StateDir 'secrets.json') -Encoding UTF8
}

# ---- 網路 -------------------------------------------------------------------
function Test-LabTcp([string]$HostName, [int]$Port, [int]$TimeoutMs = 2000) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $ar = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $ar.AsyncWaitHandle.WaitOne($TimeoutMs)) { return $false }
        $client.EndConnect($ar)
        return $true
    } catch { return $false } finally { $client.Close() }
}

function Wait-LabTcp([string]$HostName, [int]$Port, [int]$TimeoutMinutes, [string]$What) {
    $deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    $i = 0
    while ((Get-Date) -lt $deadline) {
        if (Test-LabTcp $HostName $Port) { return $true }
        if ($i % 6 -eq 0) { Write-Info ("等待 {0}（{1}:{2}）… 已等 {3} 分鐘" -f $What, $HostName, $Port, [int]($i / 6)) }
        Start-Sleep -Seconds 10
        $i++
    }
    return $false
}

function Test-LabAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal $id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-LabWinget([string]$Id, [string[]]$Extra = @()) {
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) { return $false }
    $wargs = @('install', '--id', $Id, '-e', '--accept-package-agreements', '--accept-source-agreements', '--silent') + $Extra
    & winget.exe @wargs
    # winget 已安裝時也會回非 0，交給呼叫端再檢查一次
    return ($LASTEXITCODE -eq 0)
}

Export-ModuleMember -Function *
