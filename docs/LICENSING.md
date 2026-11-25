# 授权安装与发布

本文供签发 CD-KEY 和制作 Windows 安装包的发布者使用。客户只需要交付包和对应机器的 CD-KEY。安装、配置模型服务及第一次连接报表的操作见[使用指南](USER_GUIDE.md)。

## 为客户签发 CD-KEY

1. 让客户在**目标电脑**上运行 `PowerBIAssistant-Setup-2.0.13.exe`，在“软件授权”页复制以 `PBI2M-` 开头的完整机器码。它来自 Windows `MachineGuid` 的产品专用哈希。
2. 在发布电脑的项目根目录打开 PowerShell。按授权用途选择下面一条命令，用客户的实际机器码替换引号中的示例文字。
3. 将输出的完整 CD-KEY 发给客户。客户粘贴到同一授权页，核对授权类型和试用到期时间，继续安装。发码工具另行打印的授权说明留在文本框外。

```powershell
# 永久授权，输出以 PBI2K- 开头
.\.venv\Scripts\python.exe -X utf8 -m scripts.license_admin issue --type permanent --machine-code "客户的完整机器码"

# 七天试用，输出以 PBI2T- 开头
.\.venv\Scripts\python.exe -X utf8 -m scripts.license_admin issue --type trial --machine-code "客户的完整机器码"
```

省略 `--type` 时默认签发永久授权。试用从**签发时刻**起持续 604800 秒。安装确认页与试用版窗口标题使用 UTC，北京时间为 UTC+8。同一枚试用码重装后沿用原到期日。再次签发会按新的签发时间计算，发布者可自行决定是否续发。

需要给部署工具提供 UTF-8 文件时，追加 `--out` 并指定尚未存在的文件名。例如使用 `--out "$env:USERPROFILE\Desktop\customer-cdkey.txt"`。完整参数见[签发工具](../scripts/license_admin.py)。

## 客户安装、升级和转永久

| 场景 | 操作 |
| --- | --- |
| 首次安装 | 在 Windows 10/11 x64 上运行安装器，输入本机有效 CD-KEY。安装程序按当前 Windows 用户安装，默认位置为 `%LOCALAPPDATA%\Programs\PowerBIAssistant`。运行界面依赖 Edge WebView2 Runtime，读取真实报表还需要本机 Power BI Desktop。 |
| 从旧版本升级 | 用 2.0.13 安装包覆盖原安装位置一次，授权页会带入本机原有的 CD-KEY 并重新验证。以后可在桌面窗口中更新。继续使用原公钥时，永久码和未到期的试用码均可沿用。 |
| 试用转永久 | 为原机器码签发永久码，客户关闭助手并重新运行安装器，在原位置输入新码。已过期的试用也可以从安装器直接升级。 |
| Windows 重装或换机 | 在目标系统重新取得机器码。若机器码已变更，为新码签发授权。 |

安装器将有效授权保存到程序旁的 `license.key`。会话、词典和模型服务配置等保存在单独的数据目录，安装版默认为 `%LOCALAPPDATA%\PowerBIAssistant`。设置过 `PBI_DATA_DIR` 的用户应备份该变量指定的目录。卸载程序时用户数据会保留。

授权通过 Ed25519 公钥在本机验证，源码开发入口可直接运行。安装版在启动及运行期间检查授权，试用到期会拒绝新 API 请求并中止正在执行的生成或梳理。离线校验依赖本机标识和时钟，永久码不支持远程撤销。模型服务请求及首次获取引擎依赖仍可能使用网络。

### 静默部署

将仅含目标电脑完整 CD-KEY 的 UTF-8 文件交给安装器，带 BOM 的文件也可读取。命令行传文件路径，安装器在写入应用前完成校验。安装规则见[安装脚本](../packaging/installer.iss)。

```powershell
.\dist\release-2.0.13\PowerBIAssistant-Setup-2.0.13.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART "/LICENSEFILE=C:\Deployment\customer-cdkey.txt"
```

## 发布者密钥与迁移

默认目录为 `%LOCALAPPDATA%\PowerBIAssistantPublisher\licensing`。`publisher-key.dpapi` 由当前 Windows 用户的 DPAPI 保护，用于签发。`public-key.pem` 用于构建程序，安装包只嵌入验证公钥。签发私钥、备份及客户 CD-KEY 应与对外分发文件分开保存。

**延续已有发行版时，请使用原签发密钥。** 跨电脑或用户迁移前，应在原发布账户导出密码加密的备份，妥善保管备份并另存密码。仅复制 `publisher-key.dpapi` 无法保证在新账户中解密。丢失私钥且无可用备份时，现有安装包将无法接受新签发的授权。

```powershell
# 在原发布账户导出，目标文件必须是新文件。密码交互输入，至少 12 个字符
.\.venv\Scripts\python.exe -X utf8 -m scripts.license_admin export --out "$env:USERPROFILE\Desktop\powerbi-publisher.backup.pem"

# 在新发布账户恢复，目标密钥目录不能已有发布密钥
.\.venv\Scripts\python.exe -X utf8 -m scripts.license_admin restore --backup "D:\安全备份\powerbi-publisher.backup.pem"
```

使用自定义目录时，将 `--key-dir "D:\发布密钥"` 放在 `issue`、`export` 或 `restore` 子命令前。只有建立全新独立授权体系时，才在新目录中执行 `.\.venv\Scripts\python.exe -X utf8 -m scripts.license_admin init`。发布已有产品的升级包时沿用原公钥。

## 构建并核对交付物

在 Windows 构建机准备 64 位 Python 3.11+、项目虚拟环境及 Inno Setup 6。构建脚本使用公钥生成 PyInstaller 归档，再由 Inno Setup 生成安装器。`--version` 同时设置客户端、安装器和 API 的版本，`--update-feed` 可指定客户端要检查的 HTTPS 清单地址。默认地址指向本仓库的最新 GitHub Release。详见[构建脚本](../scripts/build_desktop.py)和[打包清单](../packaging/powerbi.spec)。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -X utf8 -m scripts.build_desktop --version 2.0.13 --output-dir ".\dist\release-2.0.13"
Get-FileHash -Algorithm SHA256 -LiteralPath ".\dist\release-2.0.13\PowerBIAssistant-Setup-2.0.13.exe"
```

示例将当前源码的试构建输出到独立目录，校验时使用本次新生成的散列。默认输出位置为 `dist`，直接在其中重建相同版本可能覆盖同名交付物。默认输入公钥为发布者目录的 `public-key.pem`。使用自定义公钥时追加 `--public-key "D:\发布密钥\public-key.pem"`，可用 `--iscc` 指定编译器。

客户安装器名称为 `PowerBIAssistant-Setup-<版本>.exe`。如果另做分发 ZIP，放入安装器与对应交付版说明书，同时记录 EXE 和 ZIP 的校验值。输出目录中的 `PowerBIAssistant` 文件夹和 `LicenseVerifier.exe` 用于打包与校验。2.0.12 发行件的大小、散列和实际验证范围见[验证记录](VALIDATION.md)。

## 发布在线更新

1. 为每次发布选择高于上一版的版本号。按上一节构建最终 EXE。如果使用 Windows 代码签名，请先签好 EXE，再生成更新清单。
2. 在发布账户中运行下方命令。工具从安装包文件名读取版本，对文件大小、SHA-256、下载地址、版本号和更新说明签名，在同目录生成 `latest.json`。它使用与安装版内置公钥对应的 DPAPI 发布密钥，更新签名和授权签名使用独立的签名上下文。
3. 在当前仓库创建标签为 `v2.0.13` 的 Release 草稿，把安装包和同目录的 `latest.json` 作为两个附件一起上传，再发布并设为 **Latest**。以后将命令和标签中的版本一起递增。客户电脑需要能直接下载这两个附件。若使用其他标签或托管位置，可给签名命令传入 `--url` 指定该版本 EXE 的完整 HTTPS 地址。[GitHub Release 下载链接](https://docs.github.com/en/repositories/releasing-projects-on-github/linking-to-releases)

```powershell
.\.venv\Scripts\python.exe -X utf8 -m scripts.sign_update --installer ".\dist\release-2.0.13\PowerBIAssistant-Setup-2.0.13.exe" --notes "新增桌面在线更新"
```

客户端在启动时和持续运行期间每 6 小时后台读取固定清单，手动「检查更新」可以立即重查。下载完成后会核对文件大小和 SHA-256，启动安装器前再次校验，向安装器传入当前安装路径和本机授权文件。安装成功后会打开新版本。客户端更新请求只下载发布文件，会话、模型凭据和 CD-KEY 保留在本机。

## 验证与故障定位

日常开发可以在拥有测试依赖的虚拟环境运行授权单元测试。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q tests\test_licensing.py tests\test_trial_licensing.py
```

验证真实安装链时，在有签发权限的 Windows 发布机执行下面的命令。它需要已构建的应用目录、`LicenseVerifier.exe`、相应签发密钥和 Inno Setup，会在本机签发测试码、构建独立标识的测试安装器并执行静默安装、启动、到期和卸载检查。自定义环境可传 `--release-dir`、`--key-dir` 和 `--iscc`。完整覆盖项见[发布验证脚本](../scripts/verify_licensed_release.py)。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m scripts.verify_licensed_release --release-dir ".\dist\release-2.0.13"
```

如需用旧发行件验证过渡升级，可追加 `--previous-release-dir` 指向含旧版 `PowerBIAssistant` 目录的位置。验证完整在线升级可运行 `.\.venv\Scripts\python.exe -m tests.smoke_desktop_update`。该测试用临时签名密钥和本机 HTTPS 服务构建两个测试版本，检查原生窗口下载、退出、原位安装和重启后的会话、设置、授权及版本号。

| 现象 | 检查点 |
| --- | --- |
| 安装器拒绝 CD-KEY | 核对机器码属于当前电脑、复制的是完整授权码、签发公钥与安装包匹配。试用码还要检查目标机时间与到期时间。 |
| 安装后启动时提示授权失败 | 核对程序目录中的 `license.key`。启动失败原因写入用户数据目录下的 `assistant.log`。需要更换有效授权时重新运行安装器。 |
| Windows 提示未知发布者 | 2.0.12 发布记录未包含商业代码签名。可向分发方核对安装文件 SHA-256。CD-KEY 用于应用自身的授权校验。 |
