# Pester 5 測試：在 GitHub Actions 的 Windows（PowerShell 5.1 和 7）上跑。
# 只測不需要 VMware / Cloudflare 的部分：語法、quser 解析、閒置判斷、無人值守檔、portal.toml、ISO。

BeforeAll {
    $script:DeployDir = Split-Path -Parent $PSScriptRoot
    $script:RepoRoot = Split-Path -Parent $script:DeployDir
    foreach ($m in 'Common', 'VMware', 'Cloudflare', 'Portal') {
        Import-Module (Join-Path $script:DeployDir "lib\$m.psm1") -Force -DisableNameChecking
    }
    . (Join-Path $script:DeployDir 'guest\idle-shutdown.ps1')
}

Describe '所有腳本都能解析' {
    It '<_> 沒有語法錯誤' -ForEach @(
        Get-ChildItem -Path (Join-Path (Split-Path -Parent $PSScriptRoot) '*') -Include *.ps1, *.psm1, *.psd1 -Recurse |
            ForEach-Object { $_.FullName }
    ) {
        $errors = $null
        [void][System.Management.Automation.Language.Parser]::ParseFile($_, [ref]$null, [ref]$errors)
        $errors | Should -BeNullOrEmpty
    }

    It '檔案都有 UTF-8 BOM（Windows PowerShell 5.1 才能正確讀中文）' {
        $files = Get-ChildItem -Path $script:DeployDir -Include *.ps1, *.psm1, *.psd1 -Recurse
        foreach ($f in $files) {
            $b = [System.IO.File]::ReadAllBytes($f.FullName)
            ($b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF) | Should -BeTrue -Because $f.Name
        }
    }

    It '範例設定檔可以讀' {
        $c = Import-PowerShellDataFile (Join-Path $script:DeployDir 'lab.config.example.psd1')
        $c.VMs.Count | Should -BeGreaterThan 0
        $c.Banks[0].Name | Should -Be '示範銀行 A'
    }
}

Describe '閒置關機' {
    It '解析閒置時間 <in>' -ForEach @(
        @{ in = '.'; out = 0 }, @{ in = 'none'; out = 0 }, @{ in = '5'; out = 5 },
        @{ in = '1:05'; out = 65 }, @{ in = '2+03:04'; out = 2 * 1440 + 184 }
    ) {
        ConvertTo-IdleMinutes $in | Should -Be $out
    }

    It '解析 quser 輸出（含沒有 SESSIONNAME 的列）' {
        $lines = @(
            ' USERNAME              SESSIONNAME        ID  STATE   IDLE TIME  LOGON TIME',
            '>administrator         console             1  Active      none   11/25/2026 9:00 AM',
            ' tester                rdp-tcp#3           2  Active         12  11/25/2026 10:00 AM',
            ' pg                                        3  Disc         1:05  11/25/2026 8:00 AM'
        )
        $s = ConvertFrom-Quser $lines
        $s.Count | Should -Be 3
        $s[1].User | Should -Be 'tester'
        $s[1].IdleMinutes | Should -Be 12
        $s[2].Session | Should -Be ''
        $s[2].State | Should -Be 'Disc'
        $s[2].IdleMinutes | Should -Be 65
    }

    It 'Active 且閒置時間短才算忙碌' {
        $busy = @([pscustomobject]@{ State = 'Active'; IdleMinutes = 5 })
        $idle = @([pscustomobject]@{ State = 'Active'; IdleMinutes = 45 }, [pscustomobject]@{ State = 'Disc'; IdleMinutes = 0 })
        Test-SessionsBusy $busy 30 | Should -BeTrue
        Test-SessionsBusy $idle 30 | Should -BeFalse
        Test-SessionsBusy @() 30 | Should -BeFalse
    }

    It '閒置滿時間才關機，剛開機不關' {
        $now = Get-Date '2026-11-25T12:00:00'
        (Get-IdleDecision $now $now.AddMinutes(-40) $now.AddHours(-2) $false 30).Shutdown | Should -BeTrue
        (Get-IdleDecision $now $now.AddMinutes(-10) $now.AddHours(-2) $false 30).Shutdown | Should -BeFalse
        (Get-IdleDecision $now $now.AddHours(-5) $now.AddMinutes(-5) $false 30).Shutdown | Should -BeFalse
        $d = Get-IdleDecision $now $now.AddHours(-5) $now.AddHours(-6) $true 30
        $d.Shutdown | Should -BeFalse
        $d.LastBusy | Should -Be $now
    }
}

Describe '無人值守安裝檔' {
    It '產生合法 XML，並跳脫密碼裡的特殊字元' {
        $xml = [xml](New-LabUnattendXml -ComputerName 'CLI-01' -AdminPassword 'a<b&c"d' -ImageIndex 2)
        $ns = @{ u = 'urn:schemas-microsoft-com:unattend' }
        (Select-Xml -Xml $xml -XPath '//u:ComputerName' -Namespace $ns).Node.InnerText | Should -Be 'CLI-01'
        (Select-Xml -Xml $xml -XPath '//u:AdministratorPassword/u:Value' -Namespace $ns).Node.InnerText | Should -Be 'a<b&c"d'
        (Select-Xml -Xml $xml -XPath '//u:MetaData/u:Value' -Namespace $ns).Node.InnerText | Should -Be '2'
        (Select-Xml -Xml $xml -XPath '//u:CommandLine' -Namespace $ns).Node.InnerText | Should -Match 'setup-guest.ps1'
    }

    It '電腦名稱超過 15 字元會報錯' {
        { New-LabUnattendXml -ComputerName 'THIS-NAME-IS-TOO-LONG' -AdminPassword 'x' } | Should -Throw
    }

    It '產生的密碼符合 Windows 複雜度' {
        1..20 | ForEach-Object {
            $p = New-LabPassword
            $p.Length | Should -Be 16
            $p | Should -MatchExactly '[A-Z]'
            $p | Should -MatchExactly '[a-z]'
            $p | Should -Match '[0-9]'
            $p | Should -Match '[^A-Za-z0-9]'
        }
    }

    It 'vmx 掛上三張光碟、用 BIOS 和 NAT' {
        $vm = @{ Name = 'cli-01'; MemoryMB = 4096; Cpus = 2 }
        $vmx = New-LabVmx -Vm $vm -DiskFile 'cli-01.vmdk' -WindowsIso 'C:\iso\w.iso' -SetupIso 'C:\vm\setup.iso' -ToolsIso 'C:\vmw\windows.iso'
        $vmx | Should -Match 'firmware = "bios"'
        $vmx | Should -Match 'ethernet0.connectionType = "nat"'
        $vmx | Should -Match 'sata0:2.fileName = "C:\\vm\\setup.iso"'
    }

    It '安裝完拿掉光碟' {
        $tmp = Join-Path ([IO.Path]::GetTempPath()) 'labtest.vmx'
        $vm = @{ Name = 'cli-01'; MemoryMB = 4096; Cpus = 2 }
        New-LabVmx -Vm $vm -DiskFile 'd.vmdk' -WindowsIso 'w' -SetupIso 's' -ToolsIso 't' | Set-Content $tmp
        Remove-LabInstallMedia $tmp
        $text = Get-Content $tmp -Raw
        $text | Should -Not -Match 'sata0:[123]'
        $text | Should -Match 'scsi0:0.fileName'
        $b = [IO.File]::ReadAllBytes($tmp)
        $b[0] | Should -Not -Be 0xEF   # 不能有 BOM
    }

    It '用 IMAPI2 建 ISO' -Skip:(-not ($env:OS -eq 'Windows_NT')) {
        $src = Join-Path ([IO.Path]::GetTempPath()) 'labiso-src'
        New-Item -ItemType Directory -Path $src -Force | Out-Null
        Set-Content (Join-Path $src 'autounattend.xml') '<unattend/>'
        $iso = Join-Path ([IO.Path]::GetTempPath()) 'labtest.iso'
        New-LabIso -SourceDir $src -IsoPath $iso
        (Get-Item $iso).Length | Should -BeGreaterThan 10000
    }
}

Describe 'portal.toml' {
    BeforeAll {
        $script:Cfg = Import-PowerShellDataFile (Join-Path $script:DeployDir 'lab.config.example.psd1')
        $script:Cfg.AdminEmail = 'ray@example.com'
        $script:Vms = @(
            [pscustomobject]@{ Name = 'srv-demo-a'; Kind = 'server'; Bank = 'demo-a'; Vmx = 'C:\TestLab\VMs\srv-demo-a\srv-demo-a.vmx'; Ip = '192.168.80.10'; Ports = @(3389, 8080, 1433) },
            [pscustomobject]@{ Name = 'cli-01'; Kind = 'client'; Bank = $null; Vmx = 'C:\TestLab\VMs\cli-01\cli-01.vmx'; Ip = '192.168.80.21'; Ports = @(3389) }
        )
        $script:Cf = [pscustomobject]@{ AccountId = 'acc'; TeamDomain = 'hackray.cloudflareaccess.com'; Audience = 'aud123'
            VnetId = 'vnet-1'; UsersGroupId = 'g1'; ServerGroupId = 'g2' }
        function Read-Toml([string]$text) {
            $path = Join-Path ([IO.Path]::GetTempPath()) 'labtest.toml'
            [IO.File]::WriteAllText($path, $text, (New-Object Text.UTF8Encoding $false))
            $json = & python -c "import tomllib,json,sys; print(json.dumps(tomllib.load(open(sys.argv[1],'rb'))))" $path
            if ($LASTEXITCODE -ne 0) { throw "tomllib 解析失敗：$json" }
            return ($json -join '' | ConvertFrom-Json)
        }
    }

    It 'Cloudflare 模式' {
        $t = Read-Toml (New-LabPortalToml -Config $script:Cfg -Vms $script:Vms -Cf $script:Cf -InstallRoot 'C:\TestLab' -VmrunPath 'C:\VMware\vmrun.exe' -TokenFile 'C:\TestLab\secrets\cf.txt')
        $t.auth.mode | Should -Be 'cloudflare'
        $t.auth.audience | Should -Be 'aud123'
        $t.cloudflare_sync.enabled | Should -BeTrue
        $t.cloudflare_sync.api_token_file | Should -Be 'C:\TestLab\secrets\cf.txt'
        $t.driver.vmrun_path | Should -Be 'C:\VMware\vmrun.exe'
        $t.vms.Count | Should -Be 2
        $t.vms[0].bank | Should -Be 'demo-a'
        $t.vms[0].ports | Should -Be @(3389, 8080, 1433)
        $t.vms[0].rdp_url | Should -Be 'https://lab-rdp-srv.hackray.dev/rdp/vnet-1/192.168.80.10/3389'
        $t.vms[1].rdp_url | Should -Be 'https://lab-rdp.hackray.dev/rdp/vnet-1/192.168.80.21/3389'
        $t.users[0].email | Should -Be 'ray@example.com'
        $t.banks[0].name | Should -Be '示範銀行 A'
    }

    It '沒有 Cloudflare 時用 dev 模式' {
        $t = Read-Toml (New-LabPortalToml -Config $script:Cfg -Vms $script:Vms -Cf $null -InstallRoot 'C:\TestLab' -VmrunPath 'C:\VMware\vmrun.exe' -TokenFile 'x')
        $t.auth.mode | Should -Be 'dev'
        $t.cloudflare_sync.enabled | Should -BeFalse
    }

    It 'Python 那邊讀得進產生的設定' {
        $text = New-LabPortalToml -Config $script:Cfg -Vms $script:Vms -Cf $script:Cf -InstallRoot 'C:\TestLab' -VmrunPath 'C:\VMware\vmrun.exe' -TokenFile 'C:\t.txt'
        $path = Join-Path ([IO.Path]::GetTempPath()) 'labtest2.toml'
        [IO.File]::WriteAllText($path, $text, (New-Object Text.UTF8Encoding $false))
        Push-Location $script:RepoRoot
        try {
            & python -c "import sys; from portal.config import load_config_file; c = load_config_file(sys.argv[1]); assert len(c.vms) == 2; assert c.server_for('demo-a').name == 'srv-demo-a'" $path
            $LASTEXITCODE | Should -Be 0
        } finally { Pop-Location }
    }
}
