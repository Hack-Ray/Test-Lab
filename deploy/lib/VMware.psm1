# VMware Workstation Pro 與 Windows VM 的自動建置。
# 相容 Windows PowerShell 5.1。

# ---- VMware 本體 -------------------------------------------------------------
function Find-LabVMware {
    $dirs = @()
    foreach ($key in 'HKLM:\SOFTWARE\WOW6432Node\VMware, Inc.\VMware Workstation',
                     'HKLM:\SOFTWARE\VMware, Inc.\VMware Workstation') {
        $p = (Get-ItemProperty $key -ErrorAction SilentlyContinue).InstallPath
        if ($p) { $dirs += $p }
    }
    $dirs += "${env:ProgramFiles(x86)}\VMware\VMware Workstation", "$env:ProgramFiles\VMware\VMware Workstation"
    foreach ($d in $dirs) {
        if ($d -and (Test-Path (Join-Path $d 'vmrun.exe'))) {
            return [pscustomobject]@{
                Dir         = $d
                Vmrun       = Join-Path $d 'vmrun.exe'
                VDiskManager = Join-Path $d 'vmware-vdiskmanager.exe'
                ToolsIso    = Join-Path $d 'windows.iso'
            }
        }
    }
    return $null
}

function Install-LabVMware {
    $vm = Find-LabVMware
    if ($vm) { Write-Ok "已安裝 VMware Workstation：$($vm.Dir)"; return $vm }
    Write-Info '用 winget 安裝 VMware Workstation Pro…'
    [void](Invoke-LabWinget 'VMware.WorkstationPro')
    $vm = Find-LabVMware
    while (-not $vm) {
        Wait-LabUser -Title '安裝 VMware Workstation Pro' -Steps @(
            'winget 沒辦法自動下載（Broadcom 需要登入），請用瀏覽器開 https://support.broadcom.com 並註冊 / 登入',
            '搜尋「VMware Workstation Pro」，下載最新版 Windows 安裝檔（個人使用免費）',
            '執行安裝檔，全部用預設值；授權選「個人使用」',
            '裝好後回到這個視窗'
        )
        $vm = Find-LabVMware
    }
    Write-Ok "VMware Workstation：$($vm.Dir)"
    return $vm
}

function Get-LabNatNetwork {
    <# VMnet8（NAT）網段。host 端介面是 .1，NAT 閘道和 DNS 是 .2。 #>
    $ip = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.InterfaceAlias -like '*VMnet8*' } | Select-Object -First 1
    if (-not $ip) { throw '找不到 VMware NAT 網卡（VMnet8）。請打開 VMware 的「虛擬網路編輯器」確認 VMnet8 是 NAT 模式。' }
    if ($ip.PrefixLength -ne 24) { throw "VMnet8 的遮罩不是 /24（目前 /$($ip.PrefixLength)），請在虛擬網路編輯器改成 255.255.255.0" }
    $prefix = ($ip.IPAddress -split '\.')[0..2] -join '.'
    return [pscustomobject]@{
        Prefix = $prefix; Cidr = "$prefix.0/24"; PrefixLength = 24
        Gateway = "$prefix.2"; Dns = "$prefix.2"; HostIp = $ip.IPAddress
    }
}

# ---- Windows ISO -------------------------------------------------------------
function Get-LabWindowsIso([string]$Url, [string]$Path) {
    if ((Test-Path $Path) -and (Get-Item $Path).Length -gt 3GB) { Write-Ok "ISO 已存在：$Path"; return }
    New-Item -ItemType Directory -Path (Split-Path $Path) -Force | Out-Null
    Write-Info "下載 Windows Server 2022 評估版 ISO（約 5GB，可能要一段時間）…"
    try {
        Start-BitsTransfer -Source $Url -Destination $Path -DisplayName 'Test-Lab Windows ISO' -ErrorAction Stop
    } catch {
        Write-Note "自動下載失敗：$_"
    }
    while (-not ((Test-Path $Path) -and (Get-Item $Path).Length -gt 3GB)) {
        Wait-LabUser -Title '下載 Windows Server 2022 評估版 ISO' -Steps @(
            '用瀏覽器開 https://www.microsoft.com/evalcenter/evaluate-windows-server-2022',
            '選 ISO 下載、填表（英文版 64 位元）',
            "把下載好的 ISO 放到：$Path（檔名要一樣）"
        )
    }
    Write-Ok "ISO：$Path"
}

# ---- VMware Tools 光碟 --------------------------------------------------------
function Get-LabToolsIso($VMware, [string]$IsoDir) {
    <#
      VMware Workstation 17.6 起安裝目錄不再附 windows.iso（VMware Tools），
      找不到就從 packages.vmware.com 下載；下載失敗就不掛 Tools 光碟（VM 照樣能裝，只是少了 Tools）。
    #>
    if ($VMware.ToolsIso -and (Test-Path $VMware.ToolsIso)) { return $VMware.ToolsIso }
    $cached = Join-Path $IsoDir 'vmware-tools-windows.iso'
    if ((Test-Path $cached) -and (Get-Item $cached).Length -gt 10MB) { return $cached }
    $base = 'https://packages.vmware.com/tools/releases/latest/windows/'
    try {
        Write-Info '這版 VMware 沒附 VMware Tools 光碟，從 packages.vmware.com 下載…'
        $index = Invoke-WebRequest -Uri $base -UseBasicParsing
        $name = ([regex]::Matches($index.Content, 'VMware-tools-windows-[0-9][0-9A-Za-z\.\-]*\.iso') |
            ForEach-Object Value | Sort-Object -Unique | Select-Object -Last 1)
        if (-not $name) { throw '找不到 ISO 檔名' }
        New-Item -ItemType Directory -Path $IsoDir -Force | Out-Null
        try { Start-BitsTransfer -Source ($base + $name) -Destination $cached -ErrorAction Stop }
        catch { Invoke-WebRequest -Uri ($base + $name) -OutFile $cached -UseBasicParsing }
        Write-Ok "VMware Tools：$cached"
        return $cached
    } catch {
        # 沒有 Tools 的話：一次性密碼、主機端改密碼、網頁新增主機都不能用，所以請使用者手動補上
        Write-Note "下載 VMware Tools 失敗：$_"
        Wait-LabUser -Title '手動下載 VMware Tools 光碟' -Steps @(
            "用瀏覽器開 $base",
            '下載 VMware-tools-windows-<版本>.iso',
            "存成 $cached",
            '完成後回到這個視窗按 Enter（不想裝就直接按 Enter，VM 照樣會建好，但少了 Tools）'
        )
        if ((Test-Path $cached) -and (Get-Item $cached).Length -gt 10MB) { Write-Ok "VMware Tools：$cached"; return $cached }
        Write-Note '沒有 VMware Tools 光碟，這次建的 VM 不會有 Tools'
        return $null
    }
}

# ---- 產生無人值守檔與設定光碟 ------------------------------------------------
function ConvertTo-XmlText([string]$s) { return [System.Security.SecurityElement]::Escape($s) }

function New-LabUnattendXml {
    param(
        [Parameter(Mandatory)][string]$ComputerName,
        [Parameter(Mandatory)][string]$AdminPassword,
        [int]$ImageIndex = 2,
        [string]$TimeZone = 'Taipei Standard Time'
    )
    if ($ComputerName.Length -gt 15) { throw "電腦名稱最多 15 個字元：$ComputerName" }
    $pw = ConvertTo-XmlText $AdminPassword
    $cn = ConvertTo-XmlText $ComputerName
    $tz = ConvertTo-XmlText $TimeZone
    $c = 'processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"'
    # 在所有光碟機裡找 setup-guest.ps1（光碟機代號不固定）
    $first = 'cmd.exe /c for %d in (D E F G H I J) do if exist %d:\setup-guest.ps1 powershell.exe -NoProfile -ExecutionPolicy Bypass -File %d:\setup-guest.ps1'
    return @"
<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
  <settings pass="windowsPE">
    <component name="Microsoft-Windows-International-Core-WinPE" $c>
      <SetupUILanguage><UILanguage>en-US</UILanguage></SetupUILanguage>
      <InputLocale>en-US</InputLocale>
      <SystemLocale>en-US</SystemLocale>
      <UILanguage>en-US</UILanguage>
      <UserLocale>zh-TW</UserLocale>
    </component>
    <component name="Microsoft-Windows-Setup" $c>
      <DiskConfiguration>
        <Disk wcm:action="add">
          <DiskID>0</DiskID>
          <WillWipeDisk>true</WillWipeDisk>
          <CreatePartitions>
            <CreatePartition wcm:action="add"><Order>1</Order><Type>Primary</Type><Extend>true</Extend></CreatePartition>
          </CreatePartitions>
          <ModifyPartitions>
            <ModifyPartition wcm:action="add">
              <Order>1</Order><PartitionID>1</PartitionID><Format>NTFS</Format>
              <Label>Windows</Label><Letter>C</Letter><Active>true</Active>
            </ModifyPartition>
          </ModifyPartitions>
        </Disk>
      </DiskConfiguration>
      <ImageInstall>
        <OSImage>
          <InstallFrom>
            <MetaData wcm:action="add"><Key>/IMAGE/INDEX</Key><Value>$ImageIndex</Value></MetaData>
          </InstallFrom>
          <InstallTo><DiskID>0</DiskID><PartitionID>1</PartitionID></InstallTo>
        </OSImage>
      </ImageInstall>
      <UserData>
        <AcceptEula>true</AcceptEula>
        <FullName>Test-Lab</FullName>
        <Organization>Test-Lab</Organization>
      </UserData>
    </component>
  </settings>
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" $c>
      <ComputerName>$cn</ComputerName>
      <TimeZone>$tz</TimeZone>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-Shell-Setup" $c>
      <UserAccounts>
        <AdministratorPassword><Value>$pw</Value><PlainText>true</PlainText></AdministratorPassword>
      </UserAccounts>
      <AutoLogon>
        <Password><Value>$pw</Value><PlainText>true</PlainText></Password>
        <Enabled>true</Enabled>
        <LogonCount>1</LogonCount>
        <Username>Administrator</Username>
      </AutoLogon>
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <ProtectYourPC>3</ProtectYourPC>
      </OOBE>
      <FirstLogonCommands>
        <SynchronousCommand wcm:action="add">
          <Order>1</Order>
          <CommandLine>$(ConvertTo-XmlText $first)</CommandLine>
          <Description>Test-Lab guest setup</Description>
        </SynchronousCommand>
      </FirstLogonCommands>
    </component>
  </settings>
</unattend>
"@
}

function New-LabGuestConfig {
    param($Vm, $Net, $Config, $Secrets)
    $watch = @()
    if ($Vm.Kind -eq 'server') { $watch = @($Vm.Ports | Where-Object { $_ -ne 3389 }) }
    return [pscustomobject]@{
        Ip             = "$($Net.Prefix).$($Vm.IpSuffix)"
        PrefixLength   = $Net.PrefixLength
        Gateway        = $Net.Gateway
        Dns            = $Net.Dns
        Ports          = @($Vm.Ports)
        WatchPorts     = $watch
        TesterUser     = $Config.TesterUser
        TesterPassword = $Secrets.TesterPassword
        TesterIsAdmin  = $false
        IdleMinutes    = $Config.IdleMinutes
    }
}

$script:IsoTypeLoaded = $false
function New-LabIso([string]$SourceDir, [string]$IsoPath, [string]$Label = 'LABSETUP') {
    <# 用 Windows 內建的 IMAPI2 建 ISO，不用另外裝工具。 #>
    if (-not $script:IsoTypeLoaded) {
        if (-not ('LabIsoWriter' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Runtime.InteropServices.ComTypes;
public static class LabIsoWriter {
    public static void Write(string path, object stream, int blockSize, int totalBlocks) {
        IStream s = (IStream)stream;
        byte[] buf = new byte[blockSize];
        using (FileStream o = File.Create(path)) {
            while (totalBlocks-- > 0) {
                s.Read(buf, blockSize, IntPtr.Zero);
                o.Write(buf, 0, blockSize);
            }
        }
    }
}
'@
        }
        $script:IsoTypeLoaded = $true
    }
    $fsi = New-Object -ComObject IMAPI2FS.MsftFileSystemImage
    $fsi.FileSystemsToCreate = 3   # ISO9660 + Joliet
    $fsi.VolumeName = $Label
    $fsi.Root.AddTree($SourceDir, $false)
    $img = $fsi.CreateResultImage()
    if (Test-Path $IsoPath) { Remove-Item $IsoPath -Force }
    [LabIsoWriter]::Write($IsoPath, $img.ImageStream, $img.BlockSize, $img.TotalBlocks)
}

# ---- .vmx ---------------------------------------------------------------------
function New-LabVmx {
    param($Vm, [string]$DiskFile, [string]$WindowsIso, [string]$SetupIso, [string]$ToolsIso)
    $lines = @(
        '.encoding = "UTF-8"'
        'config.version = "8"'
        'virtualHW.version = "19"'
        "displayName = `"$($Vm.Name)`""
        'guestOS = "windows2019srvnext-64"'
        'firmware = "bios"'
        "memsize = `"$($Vm.MemoryMB)`""
        "numvcpus = `"$($Vm.Cpus)`""
        # PCI / PCIe 橋接：沒有這些，SCSI、SATA、e1000e 等 PCIe 裝置會找不到插槽
        # （VMware 會報 "No PCIe slot available for SCSI0"）
        'pciBridge0.present = "TRUE"'
        'pciBridge4.present = "TRUE"'
        'pciBridge4.virtualDev = "pcieRootPort"'
        'pciBridge4.functions = "8"'
        'pciBridge5.present = "TRUE"'
        'pciBridge5.virtualDev = "pcieRootPort"'
        'pciBridge5.functions = "8"'
        'pciBridge6.present = "TRUE"'
        'pciBridge6.virtualDev = "pcieRootPort"'
        'pciBridge6.functions = "8"'
        'pciBridge7.present = "TRUE"'
        'pciBridge7.virtualDev = "pcieRootPort"'
        'pciBridge7.functions = "8"'
        'vmci0.present = "TRUE"'
        'hpet0.present = "TRUE"'
        'floppy0.present = "FALSE"'
        'scsi0.present = "TRUE"'
        'scsi0.virtualDev = "lsisas1068"'
        'scsi0:0.present = "TRUE"'
        "scsi0:0.fileName = `"$DiskFile`""
        'sata0.present = "TRUE"'
        'sata0:1.present = "TRUE"'
        'sata0:1.deviceType = "cdrom-image"'
        "sata0:1.fileName = `"$WindowsIso`""
        'sata0:2.present = "TRUE"'
        'sata0:2.deviceType = "cdrom-image"'
        "sata0:2.fileName = `"$SetupIso`""
        'bios.bootOrder = "hdd,cdrom"'
        'ethernet0.present = "TRUE"'
        'ethernet0.connectionType = "nat"'
        'ethernet0.virtualDev = "e1000e"'
        'ethernet0.addressType = "generated"'
        'usb.present = "FALSE"'
        'sound.present = "FALSE"'
        'mks.enable3d = "FALSE"'
        'tools.syncTime = "TRUE"'
        'tools.upgrade.policy = "manual"'
        'msg.autoAnswer = "TRUE"'
    )
    if ($ToolsIso) {
        # 只有檔案確定存在才掛：找不到檔案時 VMware 會跳視窗詢問，背景（nogui）開機就會被取消
        $lines += @(
            'sata0:3.present = "TRUE"'
            'sata0:3.deviceType = "cdrom-image"'
            "sata0:3.fileName = `"$ToolsIso`""
        )
    }
    return ($lines -join "`r`n") + "`r`n"
}

function Remove-LabInstallMedia([string]$VmxPath) {
    <# 安裝完拿掉 Windows ISO 和設定光碟（設定光碟裡有密碼）。 #>
    $text = Get-Content $VmxPath -Encoding UTF8
    $out = foreach ($l in $text) {
        if ($l -match '^sata0:[123]\.') { continue }
        $l
    }
    Write-LabTextNoBom $VmxPath (($out -join "`r`n") + "`r`n")
}

function Write-LabTextNoBom([string]$Path, [string]$Text) {
    # VMware 不吃 BOM，.vmx 一律用不含 BOM 的 UTF-8
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding $false))
}

function Invoke-LabVmrun($VMware, [string[]]$Arguments) {
    $out = & $VMware.Vmrun -T ws @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw "vmrun $($Arguments -join ' ') 失敗：$out" }
    return $out
}

function Test-LabVmRunning($VMware, [string]$VmxPath) {
    $list = & $VMware.Vmrun -T ws list 2>$null
    foreach ($l in $list) { if ($l.Trim() -ieq $VmxPath) { return $true } }
    return $false
}

function New-LabVm {
    <# 建一台 VM 並跑完無人值守安裝。已建好（有 clean 快照）就跳過。 #>
    param($Vm, $VMware, $Net, $Config, $Secrets, [string]$RepoRoot)
    $dir = Join-Path $Config.VmRoot $Vm.Name
    $vmx = Join-Path $dir "$($Vm.Name).vmx"
    $ip = "$($Net.Prefix).$($Vm.IpSuffix)"
    $done = Get-LabStateValue "vm_$($Vm.Name)"
    if ($done -and (Test-Path $vmx)) { Write-Ok "$($Vm.Name) 已建好（$ip）"; return $done }

    Write-Step "建立 VM：$($Vm.Name)（$ip）"
    # 已經在跑（例如上次中斷後、或手動在 VMware 介面開機安裝中）：不要重做設定光碟和 vmx，直接等安裝完成
    $alreadyRunning = (Test-Path $vmx) -and (Test-LabVmRunning $VMware $vmx)
    $setupIso = Join-Path $dir 'setup.iso'
    if ($alreadyRunning) { Write-Info "$($Vm.Name) 已經在開機中，直接等安裝完成" } else {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        $disk = Join-Path $dir "$($Vm.Name).vmdk"
        if (-not (Test-Path $disk)) {
            & $VMware.VDiskManager -c -s "$($Vm.DiskGB)GB" -a lsilogic -t 0 $disk | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "建立虛擬硬碟失敗：$disk" }
        }

        # 設定光碟：autounattend.xml + setup-guest.ps1 + idle-shutdown.ps1 + guest-config.json
        $stage = Join-Path $dir 'setup-media'
        New-Item -ItemType Directory -Path $stage -Force | Out-Null
        New-LabUnattendXml -ComputerName $Vm.Name.ToUpper() -AdminPassword $Secrets.AdminPassword `
            -ImageIndex $Config.ImageIndex -TimeZone $Config.TimeZone |
            Set-Content -Path (Join-Path $stage 'autounattend.xml') -Encoding UTF8
        Copy-Item (Join-Path $RepoRoot 'deploy\guest\setup-guest.ps1') $stage -Force
        Copy-Item (Join-Path $RepoRoot 'deploy\guest\idle-shutdown.ps1') $stage -Force
        New-LabGuestConfig -Vm $Vm -Net $Net -Config $Config -Secrets $Secrets |
            ConvertTo-Json | Set-Content -Path (Join-Path $stage 'guest-config.json') -Encoding UTF8
        New-LabIso -SourceDir $stage -IsoPath $setupIso
        Remove-Item $stage -Recurse -Force

        New-LabVmx -Vm $Vm -DiskFile (Split-Path $disk -Leaf) -WindowsIso $Config.IsoPath `
            -SetupIso $setupIso -ToolsIso (Get-LabToolsIso $VMware (Split-Path $Config.IsoPath)) |
            ForEach-Object { Write-LabTextNoBom $vmx $_ }

    }

    Write-Info '開機並開始無人值守安裝（大約 20–40 分鐘）…'
    if (-not (Test-LabVmRunning $VMware $vmx)) {
        try { Invoke-LabVmrun $VMware @('start', $vmx, 'nogui') | Out-Null }
        catch {
            throw ("$($Vm.Name) 開機失敗：$_`n" +
                "  常見原因：(1) VMware Workstation 從沒打開過，授權視窗還沒按（請打開一次，選個人使用）；`n" +
                "  (2) VMware 跳出詢問視窗。請打開 VMware → File → Open → $vmx → Power on，看跳出什麼訊息。`n" +
                "  處理完關掉那台 VM，再重跑 deploy.ps1。")
        }
    }
    if (-not (Wait-LabTcp $ip 3389 90 "$($Vm.Name) 安裝完成")) {
        throw "$($Vm.Name) 90 分鐘內沒有完成安裝。請在 VMware 打開這台 VM 看畫面卡在哪裡，處理後重跑 deploy.ps1。"
    }
    # setup-guest.ps1 開完 RDP 會重開機一次，等它重開完
    Start-Sleep -Seconds 90
    if (-not (Wait-LabTcp $ip 3389 20 "$($Vm.Name) 重開機")) { throw "$($Vm.Name) 重開機後連不上 3389" }

    Write-Info '關機、拿掉安裝光碟、建立 clean 快照…'
    try { Invoke-LabVmrun $VMware @('stop', $vmx, 'soft') | Out-Null }
    catch { Invoke-LabVmrun $VMware @('stop', $vmx, 'hard') | Out-Null }   # 沒有 VMware Tools 時 soft 會失敗
    $deadline = (Get-Date).AddMinutes(10)
    while ((Test-LabVmRunning $VMware $vmx) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 5 }
    Remove-LabInstallMedia $vmx
    Remove-Item $setupIso -Force -ErrorAction SilentlyContinue
    Invoke-LabVmrun $VMware @('snapshot', $vmx, 'clean') | Out-Null

    $info = [pscustomobject]@{ Name = $Vm.Name; Kind = $Vm.Kind; Bank = $Vm.Bank; Vmx = $vmx; Ip = $ip; Ports = @($Vm.Ports) }
    Set-LabState "vm_$($Vm.Name)" $info
    Write-Ok "$($Vm.Name) 完成"
    return $info
}

function Invoke-LabGuestPs($VMware, [string]$Vmx, [string]$User, [string]$Password, [string]$Script, [int]$TimeoutSec = 600) {
    <# 用 vmrun runProgramInGuest 在 VM 裡跑 PowerShell。回傳 $true/$false；逾時會砍掉 vmrun 不會一直卡住。 #>
    $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Script))
    $ps = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
    $argLine = "-T ws -gu `"$User`" -gp `"$($Password -replace '"', '\"')`" runProgramInGuest `"$Vmx`" `"$ps`" `"-NoProfile -ExecutionPolicy Bypass -EncodedCommand $enc`""
    $p = Start-Process -FilePath $VMware.Vmrun -ArgumentList $argLine -NoNewWindow -PassThru
    if (-not $p.WaitForExit($TimeoutSec * 1000)) {
        try { $p.Kill() } catch { }
        Write-Note "逾時（$TimeoutSec 秒）"
        return $false
    }
    return ($p.ExitCode -eq 0)
}

function Get-LabToolsState($VMware, [string]$Vmx) {
    <# running / installed / notInstalled / unknown #>
    $out = & $VMware.Vmrun -T ws checkToolsState $Vmx 2>&1
    return ("$out").Trim()
}

Export-ModuleMember -Function *
