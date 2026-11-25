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
| 自動結束分配 | VM 被 Windows 閒置排程關掉後，reconcile 會發現並把分配標成 released。 |

這次沒做的：閒置關機（之後放在 Windows 本機用工作排程器 + `quser`）、一鍵部署、vSphere driver。

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

PoC 可以先用工作排程器「開機時執行」`python -m portal --config C:\TestLab\portal.toml`，
或用 NSSM 包成服務。一鍵部署之後再做。

## 專案結構

```
portal/
  app.py           FastAPI 路由、背景迴圈（健康檢查 + reconcile）
  service.py       核心邏輯：權限、開放時段、分配、預暖機、紀錄
  auth.py          Cloudflare Access JWT 驗證 / dev 模式
  config.py        讀 portal.toml（銀行、使用者、VM 以設定檔為準）
  db.py            SQLite：分配、延長、稽核、可在 UI 改的設定
  drivers/         VMDriver 介面、MockDriver、VmrunDriver
  static/index.html
tests/             pytest
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
