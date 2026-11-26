# 安裝 Portal：Python、venv、產生 portal.toml、註冊開機（登入）自動啟動。
# 相容 Windows PowerShell 5.1。

function Find-LabPython {
    $candidates = @("$env:ProgramFiles\Python312\python.exe", "$env:ProgramFiles\Python313\python.exe",
                    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe", "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe")
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        $p = & py.exe -3 -c "import sys; print(sys.executable if sys.version_info >= (3, 11) else '')" 2>$null
        if ($p -and (Test-Path $p)) { return $p }
    }
    return $null
}

function Install-LabPython {
    $py = Find-LabPython
    if (-not $py) {
        Write-Info '用 winget 安裝 Python 3.12…'
        [void](Invoke-LabWinget 'Python.Python.3.12' @('--scope', 'machine'))
        $py = Find-LabPython
    }
    if (-not $py) { throw '安裝 Python 失敗，請從 https://www.python.org/downloads/windows/ 安裝 3.12（勾選 Add to PATH）後重跑' }
    Write-Ok "Python：$py"
    return $py
}

function ConvertTo-TomlString([string]$s) {
    # 用 TOML 的 basic string，處理反斜線和引號
    return '"' + ($s -replace '\\', '\\' -replace '"', '\"') + '"'
}

function ConvertTo-TomlArray($items) {
    $parts = foreach ($i in @($items)) { if ($i -is [int]) { "$i" } else { ConvertTo-TomlString "$i" } }
    return '[' + ($parts -join ', ') + ']'
}

function New-LabPortalToml {
    <#
      $Cf 為 $null 時用 dev 模式（還沒接 Cloudflare 也能先在本機開 Portal）。
    #>
    param($Config, $Vms, $Cf, [string]$InstallRoot, [string]$VmrunPath, [string]$TokenFile)
    $L = New-Object System.Collections.Generic.List[string]
    $L.Add('# 由 deploy.ps1 產生。重跑部署會覆蓋這個檔案；要長期保留的修改請改 deploy\lab.config.psd1。')
    $L.Add('[portal]')
    $L.Add('timezone = "Asia/Taipei"')
    $L.Add('db_path = ' + (ConvertTo-TomlString (Join-Path $InstallRoot 'data\portal.db')))
    $L.Add('host = "127.0.0.1"')
    $L.Add('port = 8080')
    $L.Add('health_timeout_seconds = 900')
    $L.Add('')
    $L.Add('[auth]')
    if ($Cf) {
        $L.Add('mode = "cloudflare"')
        $L.Add('team_domain = ' + (ConvertTo-TomlString $Cf.TeamDomain))
        $L.Add('audience = ' + (ConvertTo-TomlString $Cf.Audience))
    } else {
        $L.Add('mode = "dev"')
        $L.Add('dev_default_user = ' + (ConvertTo-TomlString $Config.AdminEmail))
    }
    $L.Add('')
    $L.Add('[cloudflare_sync]')
    if ($Cf) {
        $L.Add('enabled = true')
        $L.Add('account_id = ' + (ConvertTo-TomlString $Cf.AccountId))
        $L.Add('users_group_id = ' + (ConvertTo-TomlString $Cf.UsersGroupId))
        $L.Add('server_group_id = ' + (ConvertTo-TomlString $Cf.ServerGroupId))
        $L.Add('api_token_file = ' + (ConvertTo-TomlString $TokenFile))
    } else { $L.Add('enabled = false') }
    $L.Add('')
    $L.Add('# 遠端桌面測試帳號：Portal 在「我的分配」顯示帳號與密碼（共用密碼每次從 secrets.json 讀）')
    $L.Add('[rdp]')
    $L.Add('username = ' + (ConvertTo-TomlString $Config.TesterUser))
    $L.Add('secrets_file = ' + (ConvertTo-TomlString (Join-Path (Split-Path -Parent $PSScriptRoot) '.state\secrets.json')))
    $L.Add('secrets_key = "TesterPassword"')
    $L.Add('# 每次分配 client 時換一組一次性密碼，釋放時換回共用密碼（需要 VM 裡有 VMware Tools）')
    $L.Add('one_time_password = true')
    $L.Add('admin_user = "Administrator"')
    $L.Add('admin_key = "AdminPassword"')
    $L.Add('')
    $L.Add('[driver]')
    $L.Add('type = "vmrun"')
    $L.Add('vmrun_path = ' + (ConvertTo-TomlString $VmrunPath))
    $L.Add('')
    $L.Add('[schedule]')
    $L.Add('weekdays = [1, 2, 3, 4, 5]')
    $L.Add('open = "08:00"')
    $L.Add('close = "17:30"')
    $L.Add('extend_roles = ["PG", "SA"]')
    $L.Add('extend_max_until = "22:00"')
    $L.Add('')
    $L.Add('[prewarm]')
    $L.Add('enabled = true')
    $L.Add('standby_clients = 0')
    $L.Add('start = "07:45"')
    $L.Add('end = "16:30"')
    $L.Add("max_running = $($Config.MaxRunning)")
    foreach ($b in $Config.Banks) {
        $L.Add('')
        $L.Add('[[banks]]')
        $L.Add('id = ' + (ConvertTo-TomlString $b.Id))
        $L.Add('name = ' + (ConvertTo-TomlString $b.Name))
    }
    $L.Add('')
    $L.Add('# 初始 SA（只在資料庫沒有使用者時匯入）')
    $L.Add('[[users]]')
    $L.Add('email = ' + (ConvertTo-TomlString $Config.AdminEmail))
    $L.Add('role = "SA"')
    $L.Add('banks = ["*"]')
    foreach ($vm in $Vms) {
        $L.Add('')
        $L.Add('[[vms]]')
        $L.Add('name = ' + (ConvertTo-TomlString $vm.Name))
        $L.Add('kind = ' + (ConvertTo-TomlString $vm.Kind))
        if ($vm.Bank) { $L.Add('bank = ' + (ConvertTo-TomlString $vm.Bank)) }
        $L.Add('vmx = ' + (ConvertTo-TomlString $vm.Vmx))
        $L.Add('host = ' + (ConvertTo-TomlString $vm.Ip))
        $L.Add('ports = ' + (ConvertTo-TomlArray @($vm.Ports | ForEach-Object { [int]$_ })))
        if ($Cf) {
            $rdpHost = if ($vm.Kind -eq 'server') { $Config.RdpServerHost } else { $Config.RdpClientHost }
            $L.Add('rdp_url = ' + (ConvertTo-TomlString "https://$rdpHost/rdp/$($Cf.VnetId)/$($vm.Ip)/3389"))
        }
    }
    return ($L -join "`n") + "`n"
}

function Install-LabPortal {
    param($Config, $Vms, $Cf, [string]$RepoRoot, [string]$InstallRoot, [string]$VmrunPath, [string]$TokenFile)
    $py = Install-LabPython
    $venv = Join-Path $InstallRoot 'venv'
    if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
        & $py -m venv $venv
        if ($LASTEXITCODE -ne 0) { throw '建立 venv 失敗' }
    }
    $vpy = Join-Path $venv 'Scripts\python.exe'
    & $vpy -m pip install --quiet --upgrade pip
    & $vpy -m pip install --quiet -r (Join-Path $RepoRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'pip install 失敗' }
    Write-Ok 'Portal 套件已安裝'

    New-Item -ItemType Directory -Path (Join-Path $InstallRoot 'data') -Force | Out-Null
    $toml = Join-Path $InstallRoot 'portal.toml'
    $text = New-LabPortalToml -Config $Config -Vms $Vms -Cf $Cf -InstallRoot $InstallRoot -VmrunPath $VmrunPath -TokenFile $TokenFile
    [System.IO.File]::WriteAllText($toml, $text, (New-Object System.Text.UTF8Encoding $false))
    & $vpy -c "import tomllib,sys; tomllib.load(open(sys.argv[1],'rb'))" $toml
    if ($LASTEXITCODE -ne 0) { throw "產生的 $toml 格式有問題" }
    Write-Ok "設定檔：$toml"

    # 用目前使用者「登入時」啟動：VMware Workstation 的 VM 屬於這個使用者，vmrun 要用同一個帳號跑
    $log = Join-Path $InstallRoot 'data\portal.log'
    # 用 pythonw.exe 跑：沒有主控台，不會留下空白視窗（Windows Terminal 會忽略 -WindowStyle Hidden）
    $vpyw = Join-Path (Split-Path $vpy) 'pythonw.exe'
    if (-not (Test-Path $vpyw)) { $vpyw = $vpy }
    $action = New-ScheduledTaskAction -Execute $vpyw `
        -Argument "-m portal --config `"$toml`" --log-file `"$log`"" -WorkingDirectory $RepoRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
    Register-ScheduledTask -TaskName 'TestLab-Portal' -Action $action -Trigger $trigger -Settings $settings `
        -User "$env:USERDOMAIN\$env:USERNAME" -RunLevel Limited -Force | Out-Null
    Stop-ScheduledTask -TaskName 'TestLab-Portal' -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName 'TestLab-Portal'
    Write-Ok '已註冊排程工作 TestLab-Portal（登入時自動啟動）'

    $ok = $false
    for ($i = 0; $i -lt 30; $i++) {
        try { Invoke-RestMethod 'http://127.0.0.1:8080/healthz' -TimeoutSec 2 | Out-Null; $ok = $true; break } catch { Start-Sleep -Seconds 2 }
    }
    if (-not $ok) { throw "Portal 沒有啟動，請看 $log" }
    Write-Ok 'Portal 已啟動：http://127.0.0.1:8080'
}

Export-ModuleMember -Function *
