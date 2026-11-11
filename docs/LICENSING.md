# 授权安装包与 CD-KEY

## 当前授权规则

- **离线一机一码，支持永久授权或 7 天试用**。同一个机器码可以按需签发两种 CD-KEY；客户粘贴后才能安装。试用期从你生成 CD-KEY 的时间开始，精确持续 **7×24 小时（604800 秒）**，不是从客户首次安装开始。
- 安装完成后，程序每次启动都会自动验证已保存的授权，无需反复输入。使用过程中也会检查试用到期：到期后拒绝新的 API 请求，正在生成的 DAX/M、分析和语义梳理停止并保留已完成的内容；不会因为一直开着软件就无限试用。浏览器预览参数、静默安装参数和数据目录环境变量都不会关闭正式安装版的授权校验。
- 使用 Ed25519 签名。发布包仅内嵌验证公钥，不包含签发私钥、发码工具、模型 API Key、用户数据库或已激活的授权文件。
- CD-KEY 较长是正常现象，请完整复制粘贴。机器码以 `PBI2M-` 开头，永久 CD-KEY 以 `PBI2K-` 开头，试用 CD-KEY 以 `PBI2T-` 开头，不要混淆。安装确认页和试用版窗口标题显示到期时间，统一标明 UTC（北京时间为 UTC+8）。
- 当前 `2.0.2` 安装包沿用 `2.0.1` 的签发公钥和授权规则，之前发出的永久 CD-KEY 继续有效，未到期试用 CD-KEY 保留原到期日；旧 `2.0.0` 安装包不支持试用 CD-KEY，试用客户必须使用新版安装包。

这是授权控制，不是无法破解的程序加密。机器码来自 Windows `MachineGuid` 的产品专用哈希，不上传原始标识；同一 Windows 安装中重装助手不会改变机器码，但重装 Windows 可能需要重新发码。克隆系统或主动修改系统标识可能绕过这种设备绑定。离线永久授权不能远程撤销，也不限制相同系统标识下的重装次数。

试用到期时间受数字签名保护，重装软件、删除数据目录或重复输入同一个试用 CD-KEY 不会重新获得 7 天。离线检查依赖系统时间；启动时间早于签发时间会被拒绝，程序本次运行中使用单调计时防止回拨时钟延长试用，但**不能完全防止重启后改时间、恢复系统快照等离线绕过**。需要强制可信时间或远程撤销时，应改用联网授权。

源码运行入口保留开发用途，不要求授权。**不要把源码或发码工具交给客户**；如果源码仓库公开，对方仍可自行运行或修改源码。此机制保护的是你发布的安装版，并不能阻止拥有源码的人自行构建。

## 你给客户发码

1. 只把 `dist\PowerBIAssistant-Setup-2.0.2.exe` 发给客户；操作说明可另附 [详细使用说明](USER_GUIDE.md)。
2. 客户运行安装包，在“软件授权”页复制机器码发给你。机器码输入框只读，但可以选中后按 Ctrl+C。
3. 你在项目根目录打开 PowerShell，根据需要运行下面的一条命令。**请复制整行，不要漏掉前面的 Python 路径。**

   永久授权：

   ```powershell
   .\.venv\Scripts\python.exe -X utf8 -m scripts.license_admin issue --type permanent --machine-code "客户发来的完整 PBI2M-机器码"
   ```

   7 天试用：

   ```powershell
   .\.venv\Scripts\python.exe -X utf8 -m scripts.license_admin issue --type trial --machine-code "客户发来的完整 PBI2M-机器码"
   ```

4. 把输出的完整 `PBI2K-...` 或 `PBI2T-...` 发给客户，客户粘贴到 CD-KEY 输入框，继续安装即可。试用发码时另行显示到期时间；不要把这段说明一起粘贴进 CD-KEY。

旧命令未指定 `--type` 时仍默认生成永久授权。每次新签发的试用密钥都从本次发码时间起算；没有“每台电脑只能试用一次”的限制，是否再次发码由你决定。不要提前批量生成试用密钥后长期存放，否则客户收到时可能只剩几天或已经过期。

也可以另存为文件，工具不会覆盖已存在的文件：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m scripts.license_admin issue `
  --type trial `
  --machine-code "客户发来的完整 PBI2M-机器码" `
  --out "$env:USERPROFILE\Desktop\customer-cdkey.txt"
```

机器码不同必须重新签发。不要把自己的机器码当作客户机器码使用。

## 试用转永久

为客户原机器码签发 `--type permanent` 的 CD-KEY，让客户先关闭助手，再重新运行新版安装包，选择原安装位置并输入永久密钥。安装程序会更新授权文件，原有模型配置、会话和业务字典不删除。过期试用无需先启动助手即可转永久。

普通版本升级也应关闭助手、备份用户数据，再运行 2.0.2 安装包覆盖原位置并输入本机有效的原 CD-KEY。无需重新初始化发布密钥，也不需要删除用户数据库。新版本包含同步模型后保留原对话的修复；仅更新源码或刷新旧窗口不能更新已安装的 EXE。详见[覆盖升级步骤](USER_GUIDE.md#从旧安装版升级到-202)。

## 签发私钥：务必备份

本次实现已为当前 Windows 用户初始化签发密钥，位置为：

```text
%LOCALAPPDATA%\PowerBIAssistantPublisher\licensing
  publisher-key.dpapi   当前 Windows 用户 DPAPI 加密的签发私钥
  public-key.pem        用于构建安装包的验证公钥
```

私钥没有放在项目或安装包中。DPAPI 文件依赖当前 Windows 用户的解密能力，**仅复制这个文件不等于可跨电脑恢复的备份**。请现在导出一份密码加密备份，密码至少 12 个字符，输入时不会显示，也不要写进命令行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m scripts.license_admin export `
  --out "$env:USERPROFILE\Desktop\powerbi-publisher.backup.pem"
```

将备份保存在安全位置，密码单独保存。不要把备份、密码或 DPAPI 私钥发给客户、上传仓库或放进发行目录。丢失签发私钥后无法继续为已有安装包签发有效 CD-KEY。

在新的 Windows 用户或电脑上恢复：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m scripts.license_admin restore `
  --backup "D:\安全备份\powerbi-publisher.backup.pem"
```

恢复会提示输入备份密码，只允许写入没有现有密钥的目录。可以在子命令前用 `--key-dir "D:\指定目录"` 指定密钥目录。重新构建发布包时也要用这个目录的公钥。

如果是第一次在全新的发布环境建立独立的授权体系，才执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m scripts.license_admin init
```

已有密钥时该命令拒绝覆盖。**升级版本应继续使用原签发密钥**，这样原来的永久 CD-KEY 才能继续使用；不要在每次发布时重新生成一套密钥。

## 重新构建安装包

构建环境需要 Windows、64 位 Python 3.11+ 和 Inno Setup 6。构建脚本只读取公钥，不解密私钥。客户电脑无需安装 Python、Node.js 或 Inno Setup。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -X utf8 -m scripts.build_desktop
```

可指定公钥及版本：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m scripts.build_desktop `
  --public-key "$env:LOCALAPPDATA\PowerBIAssistantPublisher\licensing\public-key.pem" `
  --version 2.0.2
```

输出为 `dist\PowerBIAssistant-Setup-<版本>.exe`。`build` 是中间产物；`dist` 下的应用目录和 `LicenseVerifier.exe` 是打包输入，客户只需要 Setup 安装包。

安装包按当前用户安装到 `%LOCALAPPDATA%\Programs\PowerBIAssistant`，不要求管理员权限。签名授权保存在程序旁的 `license.key` 中，用户数据继续保存在 `%LOCALAPPDATA%\PowerBIAssistant`。卸载安装版不会删除用户数据。

运行环境仍需要 Windows 10/11、Edge WebView2 Runtime；读取真实模型需要本机 Power BI Desktop。授权本身完全离线，大模型调用及原有的首次引擎依赖下载仍需要原先的网络条件。

当前没有对 EXE 做商业代码签名，Windows SmartScreen 可能显示未知发布者。CD-KEY 的数字签名与 Windows 代码签名是不同用途，不能互相替代。

## 静默部署

静默安装也必须提供本机有效的 CD-KEY 文件，省略或伪造都不能跳过验证：

```powershell
.\dist\PowerBIAssistant-Setup-2.0.2.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART `
  "/LICENSEFILE=C:\Deployment\customer-cdkey.txt"
```

授权文件使用 UTF-8，可带 BOM。密钥内容不通过进程命令行传递。不支持通过命令行禁用正式版校验。启动授权失败时，原生窗口显示错误，错误原因同时写入用户数据目录的 `assistant.log`；`--browser` 模式返回非零退出码并记录错误。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_licensing.py tests\test_trial_licensing.py tests\test_api.py tests\test_cancellation.py tests\test_background_generation.py tests\test_semantics.py tests\test_streaming.py -q
.\.venv\Scripts\python.exe -X utf8 -m scripts.verify_licensed_release
```

第二个命令需要已构建的发布目录、对应的签发私钥及 Inno Setup。它会生成仅供测试的独立安装器标识，在临时目录验证真实安装和原生窗口，然后关闭测试进程并卸载；不会替换正式安装，不调用大模型、不连接 Power BI，也不读取原有业务数据。自定义发布目录/密钥目录可传入 `--release-dir` / `--key-dir`。

已验证公钥嵌入及敏感数据排除、真实机器码一致性、两种密钥签发、精确 604800 秒边界、签发时间篡改拒绝、无授权启动拒绝、无效/缺失/跨机器/已过期/尚未生效 CD-KEY 的静默安装拒绝、试用安装与授权持久化、安装后的 API 和前端资源、原生 WebView2 窗口与到期标题、实际程序运行中到期返回 403、试用转永久后正常启动，以及删除/替换授权后的再次启动拒绝。真实到期检查使用即将到期的合法试用签名，不修改 Windows 系统时间。交互式安装向导的手动点击和视觉检查尚未执行。
