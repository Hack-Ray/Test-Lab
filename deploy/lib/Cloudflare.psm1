# 用 Cloudflare API 建立 tunnel、路由、DNS、Access 群組 / 政策 / 應用程式、瀏覽器 RDP。
# 每一步都先查有沒有，已經有就沿用，所以可以重跑。相容 Windows PowerShell 5.1。

$script:CfToken = $null
$script:CfApi = 'https://api.cloudflare.com/client/v4'

$script:TokenPermissions = @(
    'Account → Cloudflare Tunnel → Edit',
    'Account → Access: Apps and Policies → Edit',
    'Account → Access: Organizations, Identity Providers, and Groups → Edit',
    'Account → Zero Trust → Edit（建立 RDP 目標用）',
    'Account → Account Settings → Read',
    'Zone → DNS → Edit',
    'Zone → Zone → Read'
)

function Invoke-Cf {
    param([string]$Method, [string]$Path, $Body = $null)
    $headers = @{ Authorization = "Bearer $script:CfToken" }
    $params = @{ Method = $Method; Uri = "$script:CfApi$Path"; Headers = $headers; ContentType = 'application/json' }
    if ($null -ne $Body) {
        $json = $Body | ConvertTo-Json -Depth 20 -Compress
        $params.Body = [System.Text.Encoding]::UTF8.GetBytes($json)
    }
    try {
        $res = Invoke-RestMethod @params
    } catch {
        $detail = $_.ErrorDetails.Message
        if (-not $detail) { $detail = $_.Exception.Message }
        throw "Cloudflare API $Method $Path 失敗：$detail"
    }
    if ($res.PSObject.Properties.Name -contains 'success' -and -not $res.success) {
        throw "Cloudflare API $Method $Path 失敗：$($res.errors | ConvertTo-Json -Compress)"
    }
    return $res.result
}

function Initialize-LabCfToken([string]$TokenFile) {
    if ($env:CF_API_TOKEN) { $script:CfToken = $env:CF_API_TOKEN }
    elseif (Test-Path $TokenFile) { $script:CfToken = (Get-Content $TokenFile -Raw).Trim() }
    while ($true) {
        if (-not $script:CfToken) {
            Wait-LabUser -Title '建立 Cloudflare API token' -Steps (@(
                '開 https://dash.cloudflare.com/profile/api-tokens → Create Token → Create Custom Token',
                '名稱填 test-lab-deploy，權限加這幾項：'
            ) + ($script:TokenPermissions | ForEach-Object { "    $_" }) + @(
                'Account policy 的 Include 選你的帳號；Zone policy 的 Include 選 Specific zone → 你的網域（舊版畫面叫 Account / Zone Resources）',
                '建立後複製 token（只會顯示一次）'
            ))
            $script:CfToken = Read-LabFromClipboard 'Cloudflare API token'
        }
        try {
            $v = Invoke-Cf GET '/user/tokens/verify'
            if ($v.status -eq 'active') { break }
        } catch { Write-Note $_ }
        Write-Note 'token 無效，請重新輸入'
        $script:CfToken = $null
    }
    # 存起來給 Portal（同步 Access Group）和之後重跑用；資料夾只有本人能讀
    New-Item -ItemType Directory -Path (Split-Path $TokenFile) -Force | Out-Null
    Set-Content -Path $TokenFile -Value $script:CfToken -Encoding ASCII -NoNewline
    Write-Ok 'Cloudflare API token 有效'
}

# ---- 查詢小工具 ----------------------------------------------------------------
function Find-CfItem($Items, [scriptblock]$Where) { return @($Items | Where-Object $Where) | Select-Object -First 1 }

function Get-LabCfAccount($Zone) {
    # 先用 zone 帶的帳號（只要 Zone Read 就看得到），不必依賴 Account Settings: Read
    if ($Zone -and $Zone.account -and $Zone.account.id) { return $Zone.account }
    $accounts = @()
    try { $accounts = @(Invoke-Cf GET '/accounts?per_page=50') } catch { Write-Note $_ }
    if ($accounts.Count -eq 0) { throw 'token 看不到任何帳號：請編輯 token，權限加上 Account → Account Settings → Read，且 Account policy 包含你的帳號' }
    if ($accounts.Count -eq 1) { return $accounts[0] }
    for ($i = 0; $i -lt $accounts.Count; $i++) { Write-Info ("[{0}] {1}" -f $i, $accounts[$i].name) }
    return $accounts[[int](Read-Host '選擇要用的帳號編號')]
}

function Get-LabCfZone([string]$Zone) {
    $z = Find-CfItem (Invoke-Cf GET "/zones?name=$Zone") { $_.name -eq $Zone }
    if (-not $z) { throw "token 看不到 zone $Zone。請確認網域已加入 Cloudflare，且 token 的 Zone policy 有包含它" }
    return $z
}

function Get-LabCfTeamDomain([string]$AccountId) {
    while ($true) {
        try {
            $org = Invoke-Cf GET "/accounts/$AccountId/access/organizations"
            if ($org.auth_domain) { return $org.auth_domain }
        } catch { }
        Wait-LabUser -Title '啟用 Cloudflare Zero Trust' -Steps @(
            '開 https://one.dash.cloudflare.com/ 並選你的帳號',
            '第一次會要你取 team name（例如 hackray），這會變成 <team>.cloudflareaccess.com',
            '方案選 Free（50 人以內免費；可能會要求綁信用卡但不會收費）',
            '完成後回到這個視窗'
        )
    }
}

function Initialize-LabOtpIdp([string]$AccountId) {
    # 讓沒有 GitHub / Cloudflare 帳號的同事也能用 email 收驗證碼登入
    $idps = @()
    try { $idps = @(Invoke-Cf GET "/accounts/$AccountId/access/identity_providers") } catch { Write-Note $_ }
    if (Find-CfItem $idps { $_.type -eq 'onetimepin' }) { Write-Ok '登入方式 One-time PIN 已存在'; return }
    try {
        Invoke-Cf POST "/accounts/$AccountId/access/identity_providers" @{ name = 'One-time PIN'; type = 'onetimepin'; config = @{} } | Out-Null
        Write-Ok '已加入登入方式 One-time PIN（email 驗證碼）'
    } catch {
        Write-Note "無法自動加入 One-time PIN：$_"
        Write-Note '請到 Zero Trust → Settings → Authentication → Login methods → Add new → One-time PIN'
    }
}

# ---- Tunnel ----------------------------------------------------------------------
function Find-LabCloudflared {
    $cands = @(
        "${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe",
        "$env:ProgramFiles\cloudflared\cloudflared.exe",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links\cloudflared.exe"
    )
    $cmd = Get-Command cloudflared.exe -ErrorAction SilentlyContinue
    if ($cmd) { $cands += $cmd.Source }
    $pkg = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
    if (Test-Path $pkg) {
        $cands += @(Get-ChildItem $pkg -Filter cloudflared*.exe -Recurse -ErrorAction SilentlyContinue | ForEach-Object FullName)
    }
    return $cands | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}

function Install-LabCloudflared {
    $exe = Find-LabCloudflared
    if (-not $exe) {
        # 直接下載官方 MSI 安裝（不走 winget：系統管理員視窗常找不到 winget）
        Write-Info '從 GitHub 下載 cloudflared MSI 安裝…'
        $msi = Join-Path $env:TEMP 'cloudflared-windows-amd64.msi'
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            $old = $ProgressPreference; $ProgressPreference = 'SilentlyContinue'
            Invoke-WebRequest -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.msi' -OutFile $msi -UseBasicParsing
            $ProgressPreference = $old
            $p = Start-Process msiexec.exe -ArgumentList @('/i', "`"$msi`"", '/qn', '/norestart') -Wait -PassThru
            if ($p.ExitCode -ne 0) { Write-Note "msiexec 結束代碼 $($p.ExitCode)" }
        } catch { Write-Note $_ }
        $exe = Find-LabCloudflared
    }
    if (-not $exe) { throw '安裝 cloudflared 失敗，請手動從 https://github.com/cloudflare/cloudflared/releases 下載 cloudflared-windows-amd64.msi 安裝後重跑' }
    Write-Ok "cloudflared：$exe"
    return $exe
}

function Initialize-LabTunnel([string]$AccountId, [string]$Name, [string]$PortalHost) {
    $t = Find-CfItem (Invoke-Cf GET "/accounts/$AccountId/cfd_tunnel?name=$Name&is_deleted=false") { $_.name -eq $Name }
    if (-not $t) {
        $t = Invoke-Cf POST "/accounts/$AccountId/cfd_tunnel" @{ name = $Name; config_src = 'cloudflare' }
        Write-Ok "建立 tunnel $Name"
    } else { Write-Ok "沿用 tunnel $Name" }
    $token = Invoke-Cf GET "/accounts/$AccountId/cfd_tunnel/$($t.id)/token"

    # Portal 走公開 hostname；VM 走私有網段路由，不用 ingress
    Invoke-Cf PUT "/accounts/$AccountId/cfd_tunnel/$($t.id)/configurations" @{
        config = @{ ingress = @(
            @{ hostname = $PortalHost; service = 'http://127.0.0.1:8080' },
            @{ service = 'http_status:404' }
        ) }
    } | Out-Null

    $exe = Install-LabCloudflared
    if (-not (Get-Service cloudflared -ErrorAction SilentlyContinue)) {
        & $exe service install $token
        if ($LASTEXITCODE -ne 0) { throw 'cloudflared service install 失敗' }
        Write-Ok '已安裝 cloudflared 服務'
    } else {
        Write-Ok 'cloudflared 服務已存在'
    }
    Start-Service cloudflared -ErrorAction SilentlyContinue
    return $t.id
}

function Initialize-LabRoute([string]$AccountId, [string]$TunnelId, [string]$Cidr) {
    $vnets = @(Invoke-Cf GET "/accounts/$AccountId/teamnet/virtual_networks?is_deleted=false")
    $vnet = Find-CfItem $vnets { $_.is_default_network }
    $route = Find-CfItem (Invoke-Cf GET "/accounts/$AccountId/teamnet/routes?is_deleted=false&per_page=100") { $_.network -eq $Cidr }
    if (-not $route) {
        Invoke-Cf POST "/accounts/$AccountId/teamnet/routes" @{ network = $Cidr; tunnel_id = $TunnelId; comment = 'Test-Lab VMs (VMware NAT)' } | Out-Null
        Write-Ok "建立私有網段路由 $Cidr"
    } elseif ($route.tunnel_id -ne $TunnelId) {
        throw "網段 $Cidr 已經路由到另一個 tunnel（$($route.tunnel_id)），請到 Zero Trust → Networks → Routes 處理"
    } else { Write-Ok "沿用私有網段路由 $Cidr" }
    return $vnet.id
}

function Set-LabDnsCname([string]$ZoneId, [string]$Name, [string]$Target) {
    $rec = Find-CfItem (Invoke-Cf GET "/zones/$ZoneId/dns_records?name=$Name") { $_.name -eq $Name }
    $body = @{ type = 'CNAME'; name = $Name; content = $Target; proxied = $true; comment = 'Test-Lab' }
    if (-not $rec) { Invoke-Cf POST "/zones/$ZoneId/dns_records" $body | Out-Null; Write-Ok "DNS：$Name" }
    elseif ($rec.type -ne 'CNAME' -or $rec.content -ne $Target) {
        Write-Note "$Name 已經有 DNS 紀錄（$($rec.type) $($rec.content)），改成指向 tunnel"
        Invoke-Cf PUT "/zones/$ZoneId/dns_records/$($rec.id)" $body | Out-Null
    } else { Write-Ok "DNS 已存在：$Name" }
}

# ---- Access -------------------------------------------------------------------------
function Initialize-LabGroup([string]$AccountId, [string]$Name, [string]$AdminEmail) {
    $g = Find-CfItem (Invoke-Cf GET "/accounts/$AccountId/access/groups?per_page=100") { $_.name -eq $Name }
    if ($g) { Write-Ok "沿用 Access Group $Name"; return $g.id }
    # 先放第一位 SA，之後由 Portal 同步覆蓋
    $g = Invoke-Cf POST "/accounts/$AccountId/access/groups" @{ name = $Name; include = @(@{ email = @{ email = $AdminEmail } }) }
    Write-Ok "建立 Access Group $Name"
    return $g.id
}

function Initialize-LabPolicy([string]$AccountId, [string]$Name, [string]$GroupId, [switch]$Rdp) {
    $p = Find-CfItem (Invoke-Cf GET "/accounts/$AccountId/access/policies?per_page=100") { $_.name -eq $Name }
    if ($p) { Write-Ok "沿用 Access Policy $Name"; return $p.id }
    $body = @{ name = $Name; decision = 'allow'; include = @(@{ group = @{ id = $GroupId } }) }
    if ($Rdp) {
        # 剪貼簿：文字雙向可用（預設是全部禁止）
        $body.connection_rules = @{ rdp = @{
            allowed_clipboard_local_to_remote_formats = @('text')
            allowed_clipboard_remote_to_local_formats = @('text')
        } }
    }
    $p = Invoke-Cf POST "/accounts/$AccountId/access/policies" $body
    Write-Ok "建立 Access Policy $Name"
    return $p.id
}

function Get-LabApps([string]$AccountId) { return @(Invoke-Cf GET "/accounts/$AccountId/access/apps?per_page=100") }

function Initialize-LabPortalApp([string]$AccountId, [string]$PortalHost, [string]$PolicyId) {
    $app = Find-CfItem (Get-LabApps $AccountId) { $_.domain -eq $PortalHost }
    if (-not $app) {
        $app = Invoke-Cf POST "/accounts/$AccountId/access/apps" @{
            name = 'Test-Lab Portal'; type = 'self_hosted'; domain = $PortalHost
            session_duration = '24h'; app_launcher_visible = $true
            policies = @(@{ id = $PolicyId; precedence = 1 })
        }
        Write-Ok "建立 Access 應用程式 $PortalHost"
    } else { Write-Ok "沿用 Access 應用程式 $PortalHost" }
    return $app.aud
}

function Initialize-LabTarget([string]$AccountId, [string]$Name, [string]$Ip, [string]$VnetId) {
    $t = Find-CfItem (Invoke-Cf GET "/accounts/$AccountId/infrastructure/targets?hostname=$Name") { $_.hostname -eq $Name }
    $body = @{ hostname = $Name; ip = @{ ipv4 = @{ ip_addr = $Ip; virtual_network_id = $VnetId } } }
    if (-not $t) { Invoke-Cf POST "/accounts/$AccountId/infrastructure/targets" $body | Out-Null; Write-Ok "建立 RDP 目標 $Name（$Ip）" }
    else { Invoke-Cf PUT "/accounts/$AccountId/infrastructure/targets/$($t.id)" $body | Out-Null; Write-Ok "更新 RDP 目標 $Name（$Ip）" }
}

function Initialize-LabRdpApp {
    <#
      瀏覽器 RDP 的 Access 應用程式。Cloudflare 文件只有 dashboard 步驟，
      這裡用 API 的 "rdp" 類型建立；失敗的話印出手動步驟，不中斷部署。
    #>
    param([string]$AccountId, [string]$AppName, [string]$AppHost, [string[]]$TargetNames, [string]$PolicyId, [string]$PolicyName)
    $body = @{
        name = $AppName; type = 'rdp'; domain = $AppHost
        destinations = @(@{ type = 'public'; uri = $AppHost })
        target_criteria = @(@{ port = 3389; protocol = 'RDP'; target_attributes = @{ hostname = @($TargetNames) } })
        policies = @(@{ id = $PolicyId; precedence = 1 })
        session_duration = '24h'; app_launcher_visible = $true
    }
    try {
        $app = Find-CfItem (Get-LabApps $AccountId) { $_.domain -eq $AppHost }
        if ($app) { Invoke-Cf PUT "/accounts/$AccountId/access/apps/$($app.id)" $body | Out-Null; Write-Ok "更新瀏覽器 RDP 應用程式 $AppHost" }
        else { Invoke-Cf POST "/accounts/$AccountId/access/apps" $body | Out-Null; Write-Ok "建立瀏覽器 RDP 應用程式 $AppHost" }
        return $true
    } catch {
        Write-Note "用 API 建立瀏覽器 RDP 應用程式失敗：$_"
        Wait-LabUser -Title "在 dashboard 建立瀏覽器 RDP 應用程式 $AppName" -Steps @(
            '開 https://one.dash.cloudflare.com/ → Access controls → Applications → Add an application → Self-hosted and private',
            "Application name：$AppName",
            "Add public hostname：$AppHost",
            '打開「Allow access through browser-based RDP, SSH, or VNC sessions」，選 RDP',
            "Target criteria：hostname 選 $($TargetNames -join '、')，port 3389",
            "Policies：選 $PolicyName",
            '儲存'
        )
        return $false
    }
}

function Invoke-LabCloudflareSetup {
    <# 回傳 Portal 需要的 Cloudflare 設定值。 #>
    param($Config, $Vms, $Net, [string]$TokenFile)
    Initialize-LabCfToken $TokenFile
    $zone = Get-LabCfZone $Config.Zone
    $acct = Get-LabCfAccount $zone
    $team = Get-LabCfTeamDomain $acct.id
    Write-Ok "帳號 $($acct.name)、zone $($zone.name)、team $team"
    Initialize-LabOtpIdp $acct.id

    $tunnelId = Initialize-LabTunnel $acct.id 'test-lab' $Config.PortalHost
    $vnetId = Initialize-LabRoute $acct.id $tunnelId $Net.Cidr
    $target = "$tunnelId.cfargotunnel.com"
    foreach ($h in @($Config.PortalHost, $Config.RdpClientHost, $Config.RdpServerHost)) { Set-LabDnsCname $zone.id $h $target }

    $gUsers = Initialize-LabGroup $acct.id 'test-lab-users' $Config.AdminEmail
    $gServer = Initialize-LabGroup $acct.id 'test-lab-server' $Config.AdminEmail
    $pPortal = Initialize-LabPolicy $acct.id 'test-lab portal' $gUsers
    $pRdpUsers = Initialize-LabPolicy $acct.id 'test-lab rdp clients' $gUsers -Rdp
    $pRdpServer = Initialize-LabPolicy $acct.id 'test-lab rdp servers' $gServer -Rdp
    $aud = Initialize-LabPortalApp $acct.id $Config.PortalHost $pPortal

    foreach ($vm in $Vms) { Initialize-LabTarget $acct.id $vm.Name $vm.Ip $vnetId }
    $clients = @($Vms | Where-Object Kind -eq 'client' | ForEach-Object Name)
    $servers = @($Vms | Where-Object Kind -eq 'server' | ForEach-Object Name)
    if ($clients) { [void](Initialize-LabRdpApp $acct.id 'Test-Lab RDP clients' $Config.RdpClientHost $clients $pRdpUsers 'test-lab rdp clients') }
    if ($servers) { [void](Initialize-LabRdpApp $acct.id 'Test-Lab RDP servers' $Config.RdpServerHost $servers $pRdpServer 'test-lab rdp servers') }

    return [pscustomobject]@{
        AccountId = $acct.id; TeamDomain = $team; Audience = $aud; TunnelId = $tunnelId; VnetId = $vnetId
        UsersGroupId = $gUsers; ServerGroupId = $gServer
    }
}

Export-ModuleMember -Function *
