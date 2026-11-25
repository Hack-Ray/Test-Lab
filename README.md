# Test-Lab Portal（PoC）

瀏覽器遠端測試環境的 Portal：使用者從瀏覽器選銀行 → Portal 檢查權限與開放時段 →
分配 client（server 沒開就先開）→ 健康檢查通過 → 給一個 Cloudflare 瀏覽器 RDP 連結。

```
瀏覽器 ──► Cloudflare Access ──► cloudflared ──► Portal（本專案，127.0.0.1:8080）
                                     └────────► 私有 IP 的 Windows VM（瀏覽器 RDP）
```

技術：Python 3.11+、FastAPI、SQLite（單一檔案，不用另外裝資料庫）。前端是一個靜態 HTML 頁。

> 裡面的銀行、帳號、VM 全部是示範資料。不要把公司程式碼、真實電文、密碼放進這個 repo。

## 功能

| 功能 | 說明 |
|---|---|
| 權限 | 角色 PM / SE 只能進 client，PG / SA 可進 client 和 server；每個人有可進的銀行清單（`*` = 全部）。 |
| 開放時段 | 預設平日 08:00–17:30。PG / SA 可延長**當天**，要填原因、有上限（預設 22:00），延長會留紀錄。 |
| 喚醒與分配 | 每家銀行一台 server，client 一人一台。喚醒 client 時會先確保該銀行 server 開機；已開機（預暖機）的 client 優先分配。同一人換銀行沿用同一台 client。 |
| 健康檢查 | 對 VM 的 `ports` 做 TCP 檢查（client 預設 3389；server 例如 3389 / AP / SQL）。client 會連同 server 一起檢查，全部通過才變成 `ready`，逾時（預設 10 分鐘）變成 `failed`。 |
| 預暖機 | 每分鐘一次 reconcile：在設定時段內保持 N 台 client 開機待命，並受「同時開機上限」限制（保護筆電記憶體）。SA 可在 UI 改設定。 |
| 紀錄 | 分配紀錄（哪台、誰、何時開始、何時結束）、延長紀錄、稽核紀錄（含被拒絕的嘗試）。 |
| 自動結束分配 | VM 被 Windows 閒置排程關掉後，或開放時段結束（含延長）時，reconcile 會把分配標成 released。 |
| 使用者管理 | SA 在網頁上新增使用者、改角色和銀行、停用或刪除，立即生效、不用重開，每次修改都進稽核紀錄。至少會保留一位啟用中的 SA。 |
| 申請權限 | 通過 Cloudflare 登入但不在名單裡的人，會看到申請頁（選角色、銀行、填原因），SA 核准時可以調整角色和銀行。 |
| Cloudflare 同步 | 名單有變動就把啟用中的使用者同步到兩個 Access Group（全部使用者 / PG+SA），讓 Cloudflare 那層也擋住沒權限的人。 |

還沒做的：vSphere driver。

## 使用者與帳號

- **登入身分**由 Cloudflare Access 負責（公司 Google / Microsoft 帳號，或 Access 的一次性驗證碼），Portal 不存密碼。
- **角色與銀行權限**存在 Portal 的資料庫，由 SA 在「使用者管理」維護。
  `portal.toml` 的 `[[users]]` 只是**初始名單**，資料庫還沒有任何使用者時匯入一次，之後改設定檔不會再生效。
- 新人流程：新人用公司信箱通過 Access → Portal 顯示申請頁 → SA 核准 → Portal 同步到 Cloudflare Access Group。
  也可以由 SA 直接新增。

### 同步到 Cloudflare Access Group

1. 在 Zero Trust → Access → Access Groups 建兩個群組，例如 `test-lab-users`、`test-lab-server`，
   先隨便放一條 include（之後會被 Portal 覆蓋），記下兩個群組的 ID。
2. Access 應用程式的 Policy：Portal 和 client RDP 用 `test-lab-users`，server RDP 用 `test-lab-server`。
3. 建一個 API token，權限只要 **Account → Access: Organizations, Identity Providers, and Groups → Edit**，
   放進 Portal 執行環境的環境變數 `CF_API_TOKEN`。
4. 設定檔：

   ```toml
   [cloudflare_sync]
   enabled = true
   account_id = "<Cloudflare account ID>"
   users_group_id = "<test-lab-users 的 ID>"
   server_group_id = "<test-lab-server 的 ID>"
   ```

這兩個群組的 include 清單由 Portal 整個覆蓋，不要手動在 Cloudflare 改。同步失敗不會擋住 Portal 本身的修改，
錯誤會顯示在使用者管理頁並記進稽核紀錄，修好後按「立即同步」即可。

## 一鍵部署（Windows 筆電）

在一台 Windows 10/11（Home 也可以，不需要 Hyper-V）上，從零架好整套環境：

```powershell
# 以系統管理員身分開 PowerShell，在 repo 根目錄：
powershell -ExecutionPolicy Bypass -File .\deploy\deploy.ps1
```

第一次執行會把 `deploy\lab.config.example.psd1` 複製成 `deploy\lab.config.psd1`（不進 git），
網域、VM 清單、記憶體大小都在裡面改。腳本分四段，每段都能重跑，已完成的部分會跳過：

| 段落 | 做什麼 | 需要你手動的地方 |
|---|---|---|
| VMware | winget 裝 VMware Workstation Pro，確認 NAT（VMnet8）網段 | winget 下載不到時：登入 Broadcom 下載安裝檔 |
| VMs | 下載 Windows Server 2022 評估版 ISO，每台 VM 無人值守安裝：固定 IP、開 RDP、建 `tester` 帳號、裝 VMware Tools、註冊閒置關機，最後拍 `clean` 快照 | ISO 自動下載失敗時：到 evalcenter 下載 |
| Cloudflare | 用 API 建 tunnel 並裝成服務、VM 網段的私有路由、DNS、兩個 Access Group、Access Policy、Portal 的 Access 應用程式、RDP 目標、瀏覽器 RDP 應用程式 | 建 API token（腳本會列出要勾的權限）、第一次開 Zero Trust（取 team 名稱） |
| Portal | 裝 Python 3.12、venv、產生 `C:\TestLab\portal.toml`、註冊「登入時啟動」的排程工作 | 無 |

常用參數：

```powershell
.\deploy\deploy.ps1 -Stage Cloudflare     # 只重跑某一段：VMware / VMs / Cloudflare / Portal
.\deploy\deploy.ps1 -SkipCloudflare       # 先不接 Cloudflare，Portal 用 dev 模式在 http://127.0.0.1:8080
```

部署完：

- Portal：`https://lab.hackray.dev`（用你的 email 通過 Cloudflare Access 登入，你是第一位 SA）
- 瀏覽器 RDP：Portal 分配後點「開啟遠端桌面」，或從 `https://<team>.cloudflareaccess.com` 的 App Launcher 進入。
  Windows 帳號是 `tester`（或 `Administrator`），密碼在 `deploy\.state\secrets.json`（只有本人能讀，不進 git）。
- client 和 server 分成兩個 RDP 應用程式：`lab-rdp.hackray.dev`（所有使用者）、`lab-rdp-srv.hackray.dev`（只有 PG / SA）。

幾個設計上的決定：

- **主機名稱用第一層子網域**（`lab.hackray.dev`、`lab-rdp.hackray.dev`），因為 Cloudflare 免費憑證只涵蓋 `*.hackray.dev`，
  `portal.lab.hackray.dev` 這種兩層的需要付費的 Advanced Certificate。
- **VM 全部用 Windows Server 2022 評估版**（桌面體驗），不需要 TPM；評估版 180 天到期，可以重建或用 `slmgr /rearm` 延長。
- **VM 用 VMware NAT（VMnet8）網段**，固定 IP 用 `.10–.99`，cloudflared 在筆電上就能連到。
- **Portal 用「登入時啟動」**：VMware Workstation 的 VM 屬於登入的使用者，vmrun 要用同一個帳號跑，所以筆電要保持登入（可設自動登入）。
- 瀏覽器 RDP 的 Access 應用程式 Cloudflare 只有 dashboard 文件，腳本用 API 的 `rdp` 類型建立；
  如果 API 拒絕，腳本會停下來列出 dashboard 的手動步驟，做完按 Enter 繼續。

### VM 內的閒置關機

`deploy/guest/idle-shutdown.ps1` 由 VM 內的工作排程器每 5 分鐘以 SYSTEM 執行：
`quser` 沒有 Active 且閒置少於 30 分鐘的工作階段，而且 server 的 AP / SQL port 沒有外部連線，
連續 30 分鐘就關機（剛開機的 30 分鐘內不會關）。Portal 發現 VM 關機後會自動結束分配。
閒置時間在 `lab.config.psd1` 的 `IdleMinutes` 調整。

### 部署腳本的測試

`deploy/tests` 是 Pester 5 測試，CI 會在 Windows PowerShell 5.1 和 PowerShell 7 上跑：
語法檢查、quser 解析、閒置判斷、無人值守 XML、portal.toml（再用 Python 讀回來驗證）、用 IMAPI2 建 ISO。
真正的 VMware / Cloudflare 步驟需要在實機上跑。

## 本機開發（不需要 VM）

```powershell
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS：source .venv/bin/activate
pip install -r requirements-dev.txt
copy portal.example.toml portal.toml   # 沒有 portal.toml 時會直接用範例設定
python -m portal
```

打開 <http://127.0.0.1:8080>。預設是 `auth.mode = "dev"` 加 `driver.type = "mock"`：

- 右上角下拉選單可以切換示範身分（PM / SE / PG / SA），API 則用 `X-Dev-User` header 或 `?as=email`。
  想試申請流程，用 `?as=someone-new@example.com` 這種不在名單裡的 email 開頁面。
- MockDriver 開機後 `mock_boot_seconds` 秒才算健康，用來模擬 Windows 開機。
- 晚上或週末測試會被開放時段擋掉，可以暫時把 `portal.toml` 的 `close` 改晚、`weekdays` 加上 6、7。

跑測試：

```bash
python -m pytest
```

## 切換 driver

所有 VM 控制都走 `portal/drivers/base.py` 的 `VMDriver` 介面：`start / stop / status / ip / health`。

### MockDriver（預設）

```toml
[driver]
type = "mock"
mock_boot_seconds = 5
```

### VmrunDriver（VMware Workstation Pro）

```toml
[driver]
type = "vmrun"
vmrun_path = 'C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe'

[[vms]]
name = "cli-01"
kind = "client"
vmx = 'D:\VMs\cli-01\cli-01.vmx'
host = "192.168.50.11"      # 建議固定 IP；沒填會用 vmrun getGuestIPAddress（要裝 VMware Tools）
ports = [3389]
rdp_url = "https://cli-01.example.com"
```

用到的指令：`vmrun -T ws start <vmx> nogui`、`stop <vmx> soft`、`list`、`getGuestIPAddress`。
Portal 要用能操作這些 VM 的同一個 Windows 帳號執行（VM 是哪個使用者開的，`vmrun list` 才看得到）。

### 之後加 vSphere driver

1. 新增 `portal/drivers/vsphere.py`，繼承 `VMDriver`，用 pyVmomi 或 vSphere REST API 實作四個方法。
   `VMSpec.vsphere_id` 已經預留給 VM 的 moref / inventory path。
2. 在 `portal/drivers/__init__.py` 的 `create_driver` 加 `type = "vsphere"` 分支。
3. 設定檔改 `[driver] type = "vsphere"`，`[[vms]]` 填 `vsphere_id`。Portal 其他部分不用改。

## 接 cloudflared 與 Cloudflare Access

1. **Portal**：在 Cloudflare Zero Trust 建一個 Self-hosted Access 應用程式（例如 `portal.example.com`），
   Policy 只放公司的人。記下 Application Audience (AUD) tag。
2. **Tunnel**：在同一台機器裝 cloudflared，Public hostname `portal.example.com` → `http://127.0.0.1:8080`。
3. **VM**：每台 VM 建一個 Access 應用程式並開啟瀏覽器 RDP，Tunnel 指向 VM 的私有 IP `rdp://192.168.50.x:3389`，
   把網址填進該 VM 的 `rdp_url`。Access 的 Policy 也要限制只有該角色的人能進 server（Portal 和 Access 兩層都擋）。
4. **Portal 設定**改成：

   ```toml
   [auth]
   mode = "cloudflare"
   team_domain = "your-team.cloudflareaccess.com"
   audience = "<AUD tag>"
   ```

Portal 會驗證 `Cf-Access-Jwt-Assertion`（RS256，公鑰從 `https://<team>/cdn-cgi/access/certs` 取得，
並檢查 `aud`、`iss`、`exp`），再用 JWT 裡的 email 查權限表。**不會**相信
`Cf-Access-Authenticated-User-Email` 這類純文字 header。dev 模式下如果 request 帶有 `Cf-Ray`
（代表是從 Cloudflare 進來的）會直接拒絕，避免忘了切模式就對外開放。

Portal 只聽 `127.0.0.1`，不要開防火牆讓外部直連。

### 在 Windows 上常駐

一鍵部署會註冊排程工作 `TestLab-Portal`（登入時啟動、掛掉自動重啟），log 在 `C:\TestLab\data\portal.log`。

## 專案結構

```
portal/
  app.py           FastAPI 路由、背景迴圈（健康檢查 + reconcile）
  service.py       核心邏輯：權限、開放時段、分配、預暖機、紀錄
  auth.py          Cloudflare Access JWT 驗證 / dev 模式
  config.py        讀 portal.toml（銀行、VM 以設定檔為準；使用者只當初始名單）
  users.py         使用者名單與權限申請
  cloudflare.py    同步 Cloudflare Access Group
  db.py            SQLite：使用者、申請、分配、延長、稽核、可在 UI 改的設定
  drivers/         VMDriver 介面、MockDriver、VmrunDriver
  static/index.html
tests/             pytest
deploy/
  deploy.ps1                 一鍵部署入口
  lab.config.example.psd1    部署設定範例
  lib/                       VMware、Cloudflare、Portal、共用模組
  guest/                     VM 內執行：首次設定、閒置關機
  tests/                     Pester 測試
portal.example.toml
```

## API 速查

| Method | Path | 誰 |
|---|---|---|
| GET | `/api/me` | 所有人：身分、可進銀行、開放狀態 |
| POST | `/api/wake` `{bank, kind}` | 所有人（server 限 PG/SA） |
| GET | `/api/allocations[?all=true]` | 自己的；PG/SA 可看全部 |
| POST | `/api/allocations/{id}/release` | 本人或 SA |
| POST | `/api/extend` `{until, reason}` | PG/SA |
| GET | `/api/vms`、`/api/extensions`、`/api/prewarm` | PG/SA |
| PUT | `/api/prewarm` | SA |
| POST | `/api/reconcile` | SA（手動觸發一次） |
| GET | `/api/audit` | SA |
| GET | `/api/whoami` | 任何通過驗證的人（含未註冊）：自己的狀態與待審申請 |
| POST | `/api/access-requests` `{role, banks, reason}` | 未註冊的人送申請 |
| GET | `/api/access-requests[?status=pending]` | SA |
| POST | `/api/access-requests/{id}/decision` `{approve, role?, banks?}` | SA |
| GET / PUT | `/api/users` | SA |
| DELETE | `/api/users/{email}` | SA |
| GET / POST | `/api/cf-sync` | SA：查看 / 立即同步 Cloudflare |
