# 一鍵部署設定。第一次執行 deploy.ps1 會複製成 lab.config.psd1（不進 git），要改就改那份。
# 全部是示範資料；不要放公司程式碼、真實銀行或客戶資料。
@{
    # ---- 網域 -------------------------------------------------------------
    # Cloudflare 上的 zone（網域要已經在 Cloudflare 管理 DNS）
    Zone          = 'hackray.dev'
    # 用第一層子網域：Cloudflare 免費憑證只涵蓋 *.hackray.dev，
    # 像 portal.lab.hackray.dev 這種兩層的需要付費的 Advanced Certificate。
    PortalHost    = 'lab.hackray.dev'          # Portal
    RdpClientHost = 'lab-rdp.hackray.dev'      # client 的瀏覽器 RDP（所有使用者）
    RdpServerHost = 'lab-rdp-srv.hackray.dev'  # server 的瀏覽器 RDP（只有 PG / SA）

    # 第一位 SA（你的 email）。空白的話執行時會問。
    AdminEmail    = ''

    # ---- 本機路徑 ---------------------------------------------------------
    InstallRoot   = 'C:\TestLab'               # venv、Portal 設定、資料庫、ISO
    VmRoot        = 'C:\TestLab\VMs'           # VM 檔案（每台約 15–25GB，動態成長）

    # ---- Windows ISO ------------------------------------------------------
    # Windows Server 2022 評估版（180 天）。下載失敗時腳本會請你手動下載後放到 IsoPath。
    IsoUrl        = 'https://go.microsoft.com/fwlink/p/?LinkID=2195280&clcid=0x409&culture=en-us&country=US'
    IsoPath       = 'C:\TestLab\iso\WindowsServer2022Eval.iso'
    ImageIndex    = 2        # 評估版 ISO：1 Standard Core、2 Standard 桌面體驗、3 Datacenter Core、4 Datacenter 桌面體驗
    TimeZone      = 'Taipei Standard Time'

    # ---- VM 內的設定 ------------------------------------------------------
    TesterUser    = 'tester'                   # 大家共用的固定測試帳號（密碼部署時隨機產生）
    IdleMinutes   = 30                         # 沒人用多久後自動關機

    # ---- 銀行與 VM ----------------------------------------------------------
    # IP 用 VMware NAT（VMnet8）網段，IpSuffix 是最後一碼，請用 10–99（避開 DHCP 範圍）
    Banks = @(
        @{ Id = 'demo-a'; Name = '示範銀行 A' }
    )
    VMs = @(
        @{ Name = 'srv-demo-a'; Kind = 'server'; Bank = 'demo-a'; IpSuffix = 10; MemoryMB = 6144; Cpus = 2; DiskGB = 80; Ports = @(3389, 8080, 1433) }
        @{ Name = 'cli-01';     Kind = 'client';                  IpSuffix = 21; MemoryMB = 4096; Cpus = 2; DiskGB = 60; Ports = @(3389) }
    )

    # Portal 同時開機上限（32GB 筆電建議 4）
    MaxRunning    = 4
}
