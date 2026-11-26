# Test-Lab 操作手冊

這份手冊說明怎麼在一台 Windows 筆電（目前是 ASUS，32GB RAM、Windows Home）上，從零架好瀏覽器遠端測試環境，以及之後的日常操作。

```
使用者的瀏覽器
   │  https://lab.hackray.dev（Portal）
   │  https://lab-rdp.hackray.dev、lab-rdp-srv.hackray.dev（瀏覽器 RDP）
   ▼
Cloudflare Access（登入驗證）──► cloudflared（裝在筆電上）
                                     ├─► Portal（筆電 127.0.0.1:8080）
                                     └─► Windows VM（VMware NAT 私有 IP）
```

---

## 1. 哪些自動、哪些要自己做

| 項目 | 誰做 | 說明 |
|---|---|---|
| 安裝 VMware Workstation Pro | 自動（winget）／手動 | winget 抓不到時要登入 Broadcom 下載 |
| 下載 Windows Server 2022 評估版 ISO | 自動／手動 | 自動下載失敗時要到微軟評估中心下載 |
| 安裝 Windows、電腦名稱、固定 IP | 自動 | 無人值守安裝 |
| 開 RDP、防火牆（RDP / AP / SQL port） | 自動 | |
| `tester` 共用帳號、Administrator 密碼 | 自動 | 密碼隨機產生，存在 `deploy\.state\secrets.json` |
| VMware Tools、閒置自動關機排程 | 自動 | |
| `clean` 快照 | 自動 | 剛裝好、什麼都還沒裝的狀態 |
| Cloudflare API token | **手動** | 腳本會列出要勾的權限 |
| 第一次啟用 Zero Trust（取 team 名稱） | **手動** | 只有第一次 |
| Tunnel、DNS、路由、Access 群組 / 政策 / 應用程式、瀏覽器 RDP | 自動 | API 失敗時會改成列出 dashboard 手動步驟 |
| Portal 安裝、設定檔、登入時自動啟動 | 自動 | |
| **server 上的銀行 AP、SQL Server、測試資料** | **手動** | 公司的系統，腳本不會碰，見第 6 節 |
| **client 上的測試工具** | **手動** | 見第 6 節 |
| 新增使用者 | Portal 網頁 | SA 在「使用者管理」操作，會自動同步 Cloudflare |

---

## 2. 事前準備

- [ ] 筆電：建議 32GB RAM，**C 槽至少 200GB 可用空間**（ISO 5GB，每台 VM 動態成長到 20–60GB）
- [ ] Windows 10 / 11（Home 也可以，不需要 Hyper-V），用**有系統管理員權限的帳號**登入
- [ ] 網路穩定（要下載 VMware、ISO、Python，總共約 7GB）
- [ ] Cloudflare 帳號，而且 `hackray.dev` 的 DNS 已經由 Cloudflare 管理
- [ ] 你的 email（會成為第一位 SA，也是第一個能通過 Cloudflare 登入的人）
- [ ] 預留時間：第一次大約 **2 小時**，大部分是等 Windows 安裝；中間有幾次要你操作

> 部署過程筆電不要睡眠：設定 → 系統 → 電源 → 插電時「永不」睡眠。

---

## 3. 取得程式與檢查設定

1. 下載 repo：到 GitHub 的 Hack-Ray/Test-Lab 按 **Code → Download ZIP**，解壓縮到 `C:\src\Test-Lab`
   （有 git 的話：`git clone https://github.com/Hack-Ray/Test-Lab.git C:\src\Test-Lab`）。
   路徑不要有中文或空白。
2. 複製設定檔：把 `deploy\lab.config.example.psd1` 複製一份，改名為 `deploy\lab.config.psd1`。
   （不複製也可以，第一次執行時腳本會自動複製，用預設值。）
3. 用記事本打開 `deploy\lab.config.psd1`，確認這幾項：

| 設定 | 預設 | 什麼時候要改 |
|---|---|---|
| `Zone` | `hackray.dev` | 你的網域不是這個 |
| `PortalHost` / `RdpClientHost` / `RdpServerHost` | `lab.` / `lab-rdp.` / `lab-rdp-srv.hackray.dev` | 想換名字。請維持**一層**子網域，兩層（如 `portal.lab.hackray.dev`）需要付費憑證 |
| `AdminEmail` | 空白 | 可以先填你的 email，空白的話執行時會問 |
| `VMs` | 1 台 server（`srv-demo-a`）+ 1 台 client（`cli-01`） | 要更多台就照格式加；`IpSuffix` 見下方 |
| server 的 `Ports` | `@(3389, 8080, 1433)` | **AP / SQL 還沒裝之前，先改成 `@(3389)`**（見第 6.3 節） |
| `IdleMinutes` | 30 | 沒人用多久自動關機 |
| `MaxRunning` | 4 | 同時開機上限，保護記憶體 |

### IP 要填什麼？

設定檔裡**只填 IP 的最後一碼**（每台 VM 的 `IpSuffix`），完整 IP 由腳本自動組出來：

- **前三碼**是 VMware 的 NAT 網段（VMnet8），每台電腦裝 VMware 時隨機決定，例如 `192.168.80`。腳本會自動查，不用填。
- **最後一碼**填 **10–99** 之間、每台不重複的數字。預設 server `10`、client `21` → 會變成 `192.168.80.10`、`192.168.80.21`。
- 不要用 `1`、`2`（筆電本身和 NAT 閘道），也不要用 `128` 以上（VMware DHCP 會自動發的範圍）。

想先知道網段：裝好 VMware 後在 PowerShell 打 `ipconfig`，找「VMware Network Adapter VMnet8」的 IPv4 位址，前三碼就是。
部署完成畫面、Portal 的「VM 狀態」也都看得到每台 VM 的完整 IP。

---

## 4. 執行部署

1. 按「開始」→ 搜尋 **PowerShell** → 右鍵 **以系統管理員身分執行**。
2. 執行：

   ```powershell
   cd C:\src\Test-Lab
   powershell -ExecutionPolicy Bypass -File .\deploy\deploy.ps1
   ```

3. 畫面會依序顯示 `1/4` 到 `4/4`。遇到黃色框「需要你手動處理」時，照著做完再按 Enter。

> 任何時候中斷（Ctrl+C、關機、出錯），**重新執行同一個指令就會從中斷的地方接著做**，已完成的部分會跳過。
> 只想重跑某一段：`.\deploy\deploy.ps1 -Stage VMware`（或 `VMs`、`Cloudflare`、`Portal`）。

### 4.1 第 1 段：VMware

- 腳本先用 winget 安裝 VMware Workstation Pro。
- 如果出現「安裝 VMware Workstation Pro」的提示，代表 winget 抓不到，要手動：
  1. 開 <https://support.broadcom.com>，註冊或登入 Broadcom 帳號
  2. 搜尋 **VMware Workstation Pro**，下載最新版 Windows 安裝檔（個人使用免費）
  3. 執行安裝檔，全部用預設值，授權選「個人使用」
  4. 回到 PowerShell 按 Enter
- 完成後會顯示 `VM 網段：192.168.xx.0/24`。這是 VMware 的 NAT 網段，每台電腦不一樣，不用改。

### 4.2 第 2 段：建立 Windows VM

- 先下載 Windows Server 2022 評估版 ISO（約 5GB）。如果自動下載失敗：
  1. 開 <https://www.microsoft.com/evalcenter/evaluate-windows-server-2022>
  2. 選 ISO、填表，下載英文版 64 位元
  3. 把檔案放到 `C:\TestLab\iso\WindowsServer2022Eval.iso`（檔名要一樣），回去按 Enter
- 接著每台 VM 會自動安裝，**每台 20–40 分鐘**，畫面每分鐘會顯示「等待 … 安裝完成」。
- 想看安裝畫面：打開 VMware Workstation → File → Open → `C:\TestLab\VMs\<VM 名稱>\<VM 名稱>.vmx`。**只看，不要操作**。
- 每台完成後會自動關機、拿掉安裝光碟、拍 `clean` 快照。

### 4.3 第 3 段：Cloudflare

**(1) 建立 API token**（只有第一次）

1. 開 <https://dash.cloudflare.com/profile/api-tokens> → **Create Token** → 最下面 **Create Custom Token** → Get started
2. Token name：`test-lab-deploy`
3. 勾選這 7 項權限：類別是 Account 的在 **Account policy** 區塊勾，Zone 的在 **Zone policy** 區塊勾（可以用「Search for permission groups」搜尋名稱）。舊版畫面則是在 Permissions 按 **+ Add more** 一項一項加：

   | 類別 | 項目 | 權限 |
   |---|---|---|
   | Account | Cloudflare Tunnel | Edit |
   | Account | Access: Apps and Policies | Edit |
   | Account | Access: Organizations, Identity Providers, and Groups | Edit |
   | Account | Zero Trust | Edit |
   | Account | Account Settings | Read |
   | Zone | DNS | Edit |
   | Zone | Zone | Read |

4. 指定這組 token 能用在哪裡（新版畫面把權限分成兩個區塊，各自最上面有一列 **Include** 下拉）：
   - **Account policy** 區塊：Include → 選你的帳號（或 All accounts）
   - **Zone policy** 區塊：Include → **Specific zone** → `hackray.dev`（選 All zones 也能用，但範圍比較大）

   > 舊版畫面叫做「Account Resources」和「Zone Resources」，在權限清單下方，意思一樣。
5. Continue to summary → Create Token，**複製 token**（只會顯示一次）
6. token 複製好之後，回到 PowerShell 按 Enter，腳本會從剪貼簿讀取（不會顯示在畫面上，讀完會清掉剪貼簿）

   > 另一種方式：在執行 deploy.ps1 的同一個 PowerShell 視窗先輸入
   > `$env:CF_API_TOKEN = '你的token'`，再執行 deploy.ps1，腳本就會直接使用。

token 會存在 `C:\TestLab\secrets\cf_api_token.txt`（只有你的帳號能讀），之後重跑不用再輸入。

**(2) 啟用 Zero Trust**（只有第一次，如果腳本提示）

1. 開 <https://one.dash.cloudflare.com/>，選你的帳號
2. 取 team name（例如 `hackray`），之後登入頁會是 `hackray.cloudflareaccess.com`
3. 方案選 **Free**（50 人以內免費；可能要求綁信用卡，但不會收費）
4. 回到 PowerShell 按 Enter

**(3) 自動建立的東西**（不用操作，供參考）

- Tunnel `test-lab`，並在筆電上裝成 `cloudflared` 服務
- VM 網段的私有路由
- DNS：`lab`、`lab-rdp`、`lab-rdp-srv` 三筆 CNAME
- Access Group：`test-lab-users`（所有使用者）、`test-lab-server`（PG / SA）
- Access Policy、Portal 的 Access 應用程式
- RDP 目標（每台 VM 一個）、兩個瀏覽器 RDP 應用程式（client 用、server 用）

**(4) 如果出現「在 dashboard 建立瀏覽器 RDP 應用程式」**

代表 Cloudflare API 不接受自動建立，請照畫面上的步驟在 dashboard 建立（Applications → Add an application → Self-hosted and private → 打開 browser-based RDP），做完按 Enter。

### 4.4 第 4 段：Portal

- 自動安裝 Python 3.12、Portal 套件，產生 `C:\TestLab\portal.toml`，並註冊排程工作 `TestLab-Portal`（**登入 Windows 時自動啟動**）。
- 看到 `Portal 已啟動` 就完成了。

### 4.5 完成畫面

最後會顯示 Portal 網址、每台 VM 的 IP，以及密碼檔位置 `deploy\.state\secrets.json`。
用記事本打開可以看到：

- `AdminPassword`：每台 VM 的 `Administrator` 密碼
- `TesterPassword`：每台 VM 的共用測試帳號 `tester` 密碼

> 這個檔案**不要**放進 git、不要傳到聊天軟體。要給同事 tester 密碼，請當面或用公司的密碼管理工具。

---

## 5. 第一次登入驗證

1. 用瀏覽器開 <https://lab.hackray.dev>
2. 出現 Cloudflare 登入頁 → 輸入你的 email → 收信拿驗證碼登入
   - 如果登入頁沒有「寄驗證碼」的選項：到 Zero Trust → Settings → Authentication → Login methods → Add new → **One-time PIN**
3. 進到 Portal，右上角應該顯示你的 email（SA）
4. 喚醒 client：選銀行 →「Client」→ **喚醒**。狀態會從 `starting` 變成 `ready`（VM 沒開機的話要 2–5 分鐘）
5. 按 **開啟遠端桌面** → 瀏覽器裡出現 Windows 登入畫面
   - 使用者名稱：`.\tester`（前面的 `.\` 代表本機帳號）
   - 密碼：`secrets.json` 的 `TesterPassword`
6. 看到 Windows 桌面就成功了

---

## 6. 初始化 server 與 client（要自己做）

腳本只把「乾淨的 Windows」準備好，裡面的系統要自己裝。

### 6.1 server（每家銀行一台）

1. 在 Portal 選「Server」→ 喚醒（只有 PG / SA 看得到），開啟遠端桌面，用 `.\Administrator` 登入
2. 安裝銀行系統需要的東西，例如：
   - SQL Server（建議 SQL Server Developer 或 Express 版）
   - AP（應用程式伺服器、IIS、.NET 執行環境等）
   - 匯入**測試用**資料（不要放真實客戶資料）
3. 確認 AP 和 SQL 的 port 跟 `lab.config.psd1` 裡這台 server 的 `Ports` 一致（預設 8080、1433）。
   防火牆的這兩個 port 腳本已經打開；用了別的 port 的話，要在 VM 裡的「具有進階安全性的 Windows Defender 防火牆」自己開。

### 6.2 client

1. 在 Portal 喚醒 client，用 `.\Administrator` 登入
2. 安裝測試要用的工具，設定連到 server 的位址（server 的 IP 在部署完成畫面、或 Portal 的「VM 狀態」）
3. 確認用 `.\tester` 登入也能正常使用

### 6.3 健康檢查的 port

Portal 喚醒 server 時，會等 `Ports` 裡**所有 port 都能連**才顯示 `ready`。
AP 和 SQL 還沒裝之前 8080、1433 不會通，喚醒會等到逾時（15 分鐘）變成 `failed`。

- **還沒裝 AP / SQL**：把 `lab.config.psd1` 裡 server 的 `Ports` 改成 `@(3389)`，然後執行
  `.\deploy\deploy.ps1 -Stage Portal`
- **裝好之後**：改回 `@(3389, 8080, 1433)`（或實際用的 port），再跑一次 `-Stage Portal`

### 6.4 裝好後拍快照

全部裝好、確認能用之後，拍一個基準快照，之後環境弄壞可以還原：

1. 先在 VM 裡關機（開始 → 電源 → 關機）
2. VMware Workstation → 打開這台 VM → VM → Snapshot → **Take Snapshot**，名稱 `baseline`

---

## 7. 日常操作

| 要做的事 | 怎麼做 |
|---|---|
| 新增使用者 | Portal →「使用者管理」→ 新增使用者（email、角色、銀行）。會自動同步到 Cloudflare，對方就能登入 |
| 審核申請 | 不在名單的人登入後會看到申請頁；SA 在「待審核的權限申請」核准或拒絕 |
| 停用某人 | 使用者管理 → 取消「啟用」→ 儲存（保留紀錄，比刪除好） |
| 延長今天的開放時段 | PG / SA 在「開放時段」填時間和原因 → 延長（最晚 22:00） |
| 看誰在用哪台 | PG / SA 看「VM 狀態」和「全部分配紀錄」 |
| 預暖機 | SA 在「預暖機設定」調整待命台數和時段 |
| 稽核紀錄 | SA 看「稽核紀錄」，所有操作和被拒絕的嘗試都在裡面 |

角色權限：PM / SE 只能用 client；PG / SA 可以用 client 和 server；SA 可以管理使用者和設定。

自動行為：

- 開放時段（平日 08:00–17:30，含延長）結束後，使用中的分配會自動標成結束
- VM 沒人用超過 30 分鐘會自己關機，Portal 會自動結束那筆分配
- 筆電重開機、登入 Windows 後，Portal 會自動啟動；cloudflared 是系統服務，開機就會跑

> Portal 是「登入 Windows 時」啟動，筆電要保持登入。可以設定自動登入：
> 按 Win+R 輸入 `netplwiz`（Windows Home 可能要先到 設定 → 帳戶 → 登入選項 關掉「只允許 Windows Hello 登入」）。

---

## 8. 新增 VM 或銀行

1. 編輯 `deploy\lab.config.psd1`：
   - 新銀行：在 `Banks` 加一行 `@{ Id = 'demo-b'; Name = '示範銀行 B' }`
   - 新 VM：在 `VMs` 加一行，`Name` 最多 15 字元、`IpSuffix` 不重複；server 要填 `Bank`
2. 以系統管理員執行 `.\deploy\deploy.ps1`（整個跑一次）。已存在的 VM 會跳過，只建新的，並更新 Cloudflare 和 Portal 設定。
3. 新的 server / client 一樣要做第 6 節的初始化。

使用者名單存在 Portal 的資料庫，重跑部署**不會**影響使用者。

---

## 9. 疑難排解

| 狀況 | 原因與處理 |
|---|---|
| 執行 deploy.ps1 出現「無法載入，因為這個系統上已停用指令碼執行」 | 指令要包含 `-ExecutionPolicy Bypass`，照第 4 節的指令執行 |
| 中文變亂碼 | 用 Windows 內建 PowerShell 執行即可；如果是自己改過的 `.psd1`，存檔時編碼選「UTF-8 with BOM」 |
| `vmrun start … 失敗：Error: The operation was canceled` | VMware 在等人回答視窗。先從開始選單打開一次 VMware Workstation，按掉第一次的授權視窗（個人使用）；再 File → Open 那台 VM 的 `.vmx` → Power on，看跳出什麼訊息並處理，關掉 VM 後重跑 deploy.ps1 |
| VMware 跳出 `No PCIe slot available for SCSI0` | 舊版腳本產生的 vmx 少了 PCIe 橋接設定。更新到新版後，在 VMware 關掉那台 VM 的分頁，重跑 deploy.ps1（會重新產生 vmx） |
| 「找不到 VMware NAT 網卡（VMnet8）」 | 開 VMware → Edit → Virtual Network Editor → Change Settings → 確認 VMnet8 是 NAT，遮罩 255.255.255.0 |
| VM 安裝超過 90 分鐘沒完成 | 在 VMware 打開那台 VM 看畫面。常見：ISO 檔不完整（刪掉重下載）、`ImageIndex` 選到 Core 版（要用 2 或 4）。處理後刪掉 `C:\TestLab\VMs\<名稱>` 資料夾再重跑 |
| 貼上 token 時一次只能貼一個字 | 舊版腳本用隱藏輸入，Windows PowerShell 5.1 不支援整段貼上。更新到新版（改成從剪貼簿讀取），或先在同一個視窗設定 `$env:CF_API_TOKEN = '你的token'` 再重跑 |
| 「token 看不到任何帳號」 | token 少了 Account Settings: Read。到 API Tokens 編輯這把 token 補上這項權限（token 字串不變，不用重貼），再跑 `-Stage Cloudflare`。新版腳本會改從網域找帳號，通常不會再遇到 |
| Cloudflare「token 無效」 | token 複製不完整或權限不夠，照 4.3 重建 |
| Cloudflare「看不到 zone」 | token 的 Zone policy（舊版叫 Zone Resources）沒包含 `hackray.dev` |
| 開 `lab.hackray.dev` 顯示 502 / 1033 | Portal 或 cloudflared 沒在跑。看 `services.msc` 的 cloudflared 是否「執行中」；Portal 看 `C:\TestLab\data\portal.log` |
| 登入後 Portal 顯示「不在權限表裡」 | 用來登入的 email 跟 SA 的 email 不一樣，或還沒被加入名單 |
| 喚醒後一直 `starting`，最後 `failed` | server 的 AP / SQL port 沒開（見 6.3），或 VM 卡在開機。到 Portal 的稽核紀錄看 `health_timeout` 是哪個 port |
| 「同時開機的 VM 已達上限」 | 開機的 VM 太多。等閒置的自動關機，或 SA 調高「開機上限」（注意記憶體） |
| 瀏覽器 RDP 打不開、顯示沒有權限 | 這個人不在對應的 Access Group。server 只有 PG / SA 能進 |
| RDP 登入失敗 | 帳號要打 `.\tester`；密碼看 `secrets.json` |
| 想重建某台 VM | VMware 關機、刪掉 `C:\TestLab\VMs\<名稱>` 資料夾，再重跑 `.\deploy\deploy.ps1 -Stage VMs` |

Log 位置：

- 部署狀態：`deploy\.state\state.json`
- Portal：`C:\TestLab\data\portal.log`
- VM 內首次設定：VM 裡的 `C:\TestLab\setup-guest.log`
- VM 內閒置判斷：VM 裡的 `C:\TestLab\idle-state.json`

---

## 10. 維護

- **評估版到期**：Windows Server 評估版 180 天。到期前在 VM 裡用系統管理員執行 `slmgr /rearm` 後重開機，可以再延長；或刪掉重建。
- **還原 VM**：VMware → 選 VM → VM → Snapshot → Snapshot Manager → 選 `baseline`（或 `clean`）→ Go To。
- **備份**：Portal 的資料（使用者、分配、稽核）都在 `C:\TestLab\data\portal.db`，定期複製一份即可。
- **更新程式**：下載新版 repo 覆蓋 `C:\src\Test-Lab`，然後執行 `.\deploy\deploy.ps1 -Stage Portal`。
- **更換 Cloudflare token**：刪掉 `C:\TestLab\secrets\cf_api_token.txt`，重跑 `-Stage Cloudflare` 會要你輸入新的。
- **暫停整個環境**：工作排程器停用 `TestLab-Portal`；`services.msc` 停止 `cloudflared`。外部就連不進來了。

---

## 11. 安全提醒

- 不要把公司程式碼、真實電文、真實客戶資料放進 repo 或 VM 的測試資料。
- `deploy\.state\`、`C:\TestLab\secrets\` 裡有密碼和 token，不要複製到其他地方。
- Cloudflare Access 和 Portal 是兩層檢查：要拿掉某人的權限，在 Portal 停用即可（會同步移出 Access Group）。
