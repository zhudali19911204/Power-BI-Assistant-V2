# Power BI Assistant V2

面向 Windows / Power BI Desktop 的本地桌面助手：基于真实模型结构和业务语义，生成 DAX、辅助 Power Query，并通过实际查询回答数据分析问题。

**基本原则：模型在本机读取，AI 在所配置的服务端推理；生成不等于执行，验证不等于业务正确，写入必须由用户确认。**

## 文档导航

| 你要做什么 | 阅读入口 |
| --- | --- |
| 安装、配置模型、使用各项功能 | [详细使用说明](docs/USER_GUIDE.md) |
| 理解整个软件，或让其他 agent 接手开发 | [技术架构与开发接手指南](docs/ARCHITECTURE.md) |
| 签发 CD-KEY、升级、构建安装包 | [授权与发布说明](docs/LICENSING.md) |
| 查看实际验证范围、历史测试和安装包记录 | [验证记录](docs/VALIDATION.md) |

## 当前功能

左侧菜单依次为 **Power Query、DAX 助手、数据分析助手**；启动时仍默认进入 DAX 助手。

| 能力 | 已实现的行为 |
| --- | --- |
| Power Query | 基于当前查询、输出字段及共享查询／参数生成、解释或优化 M；静态检查后复制到 Desktop 高级编辑器 |
| DAX 助手 | 专业提示词、真实结构和语义共同约束生成；支持度量值、计算表、计算列；静态检查、实跑或行上下文抽样、最多两轮自动修复 |
| 校准与受控写入 | 度量值可在组合筛选下比较一个预期结果；确认时可修改度量值／计算列名称与所属表，重新验证后写入 |
| 数据分析 | 先明确口径、按需检查字段取值，再规划并执行只读 DAX；结论引用实际查询，保留结果与 CSV |
| 语义层 | 业务名称、含义、单位、角色、别名及确认状态；跨报表实例复用，模型变化后增量更新，不强制重建整份词典 |
| 模型连接与同步 | 识别 Desktop 实例、自动重连、核对结构；同步当前报表后保留原对话、历史、图片与草稿 |
| 多模型服务 | OpenAI 兼容 / Anthropic Messages；独立保存配置，卡片直接切换低／高思考强度，按接口能力明确限制 |
| 对话与图片 | 多轮对话、截图／图片、Markdown、历史导出、后台生成、进度恢复和独立中止 |

### 明确的边界

- 不直接解析或上传 PBIX 来替代实时模型连接，不是 Power BI Service 连接器。
- 不自动继承报表页面的切片器或视觉对象筛选；分析范围需要明确指定。
- 不从外部写回或刷新 Power Query M；实际应用由用户在 Desktop 中操作。
- 度量值同名覆盖须明确允许；计算表和计算列仅支持新建。计算列抽样通过不代表全量行、存储模式和循环依赖已验证。
- 示例模型只用于体验及静态检查，不能冒充真实取数或实跑。
- 可视化看板生成、多个看板组合、原生 Power BI 报表页面生成目前**尚未实现**。

### 当前发行版本

本文描述 **2026-11-12 当前源码**。最新安装包为 **2.0.4**，包含计算列、按问题检索语义、业务词典扩展与自定义名称、独立词典管理页面、分批数据分析及按时间和已有证据继续字段查值。

2.0.4 已包含字段查值优化：取消固定两轮限制，复用完整取值，达到预算后利用已有证据继续查询或生成候选确认问题。

后续修改源码**不会自动更新已有安装包**。源码提交标签、API 版本字符串与安装包构建时间不能相互替代；交付范围及校验值以[验证记录](docs/VALIDATION.md)为准。

## 快速开始

### 安装版用户

1. 从可信发布者获取 `PowerBIAssistant-Setup-2.0.4.exe`，按机器码申请永久或 7 天试用 CD-KEY。
2. 安装后打开助手，配置自己的模型服务；安装包不附带其他用户的 Key、报表或历史会话。
3. 在本机 Power BI Desktop 打开目标报表，再通过助手连接。

安装版不需要 Python 或 Node.js，需要 Windows 10/11 和 Edge WebView2 Runtime。升级前关闭助手、备份数据，使用有效原 CD-KEY 覆盖原安装位置；旧永久授权继续有效，试用到期日不重置。详见[覆盖升级步骤](docs/USER_GUIDE.md#从旧安装版升级到-204)。

### 源码运行

需要 Windows、Python 3.11+；项目已使用 Python 3.12 验证。读取真实模型还需要本机 Power BI Desktop。

在项目根目录的 PowerShell 中执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
.\.venv\Scripts\python.exe desktop.py
```

环境准备完成后也可以使用 [start.bat](start.bat)。前端是原生 JavaScript ES Modules，**正常运行无需 npm 构建，不依赖 Streamlit 或其他旧项目目录**。

浏览器开发预览：

```powershell
.\.venv\Scripts\python.exe desktop.py --browser --port 8765
```

服务只监听 `127.0.0.1`；不指定端口时自动分配。`--no-open` 可避免自动打开浏览器。关闭整个桌面应用会停止本地后端，后台生成不会在应用退出后继续。

## 推荐使用流程

1. **配置 AI 服务**：填写协议、API 根地址、模型 ID 和 Key，测试连接。无鉴权的自建服务可填 `local`；它不是云端服务的通用密钥。
2. **连接正确的报表**：核对报表名称、路径和右侧字段，不仅凭端口选择。
3. **准备业务语义**：首次没有已有解释时，DAX／数据分析需先建立初始词典；关键业务定义应人工审阅。已有历史语义时可以继续提问，新增或变化项按需增量梳理。
4. **选择工作空间**：生成 DAX、处理 M，或根据真实查询分析业务数据。涉及含糊口径时先确认，不把模型推测当成业务事实。
5. **审阅结果**：检查单位、筛选范围、查询依据和验证状态。DAX 写入前再次确认；M 复制到 Desktop 中应用。
6. **跟随模型变化**：同一报表改变后使用“同步模型”，在原对话继续；主动选择另一个报表仍进入新对话。同步前结果作为历史保留，不冒充当前数据。
7. **保存报表**：写入模型后，在 Desktop 按 Ctrl+S 保存。

思考强度默认低；支持原生强度的模型发送 `low/high`，仅支持开关的接口以“低强度=标准回复、高强度=开启思考”兼容。未知或固定思考版本明确显示不可调，不自动探测或盲目重试参数。详见[模型配置说明](docs/USER_GUIDE.md#思考强度与等待时间)。

## 数据、网络与备份

| 内容 | 默认位置／处理方式 |
| --- | --- |
| 源码版用户数据 | 项目根目录下的 `data` |
| 安装版用户数据 | `%LOCALAPPDATA%\PowerBIAssistant` |
| 自定义数据目录 | 环境变量 `PBI_DATA_DIR`，优先于默认路径 |
| 配置、模型快照、语义、会话、图片、结果和审计 | 数据目录内的 `assistant.sqlite3` |
| 模型 Key 的本地加密材料 | 同目录的 `secret.key`；备份数据库时必须一并保留 |
| 日志 | 同目录的 `assistant.log`；可能包含错误和模型信息，分享前脱敏 |
| 安装版授权 | 程序目录旁的 `license.key`，与用户数据库分开 |

**“本地桌面软件”不等于“数据完全不出本机”。** 生成时会向你配置的 AI 服务发送相关结构、公式、语义、对话及图片；数据分析还会发送受限的实际查询结果，字段证据和验证错误也可能进入后续请求。请使用获准处理相应数据的服务。

图片先在本机校验和规范化；每条消息最多 4 张，单张上传不超过 8 MB。切页保留草稿和待发送附件，但这些 UI 草稿并非持久化会话；重启前请先发送或另行保存。

备份前关闭助手，再备份实际数据目录。安装版与源码版默认目录不同；不要因为换启动方式看不到历史就删除旧数据。API Key、授权私钥、用户数据库及业务截图不得放入源码或发行包。

可通过环境变量提供初始模型配置：`PBI_LLM_PROVIDER`、`PBI_LLM_MODEL`、`PBI_LLM_BASE_URL`、`PBI_LLM_API_KEY`。界面保存的配置优先；[.env.example](.env.example)只是参考，程序**不会自动加载 `.env`**。

## 开发与测试

接手实现前先读[技术架构文档](docs/ARCHITECTURE.md)：其中包含真实入口、完整请求链、数据表关系、状态归属、重要限制和按任务选择测试的索引。

开发环境首次准备及全量回归：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
npm ci --ignore-scripts
npm test
```

Node.js 仅用于前端开发与测试；版本需支持 [package.json](package.json) 使用的 `--test-isolation=none` 参数。前端测试使用 `node:test` 和 LinkeDOM，Markdown 渲染资源随应用本地打包。

日常改动优先运行相关测试。例如同步会话：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_conversation_sync.py -q
node --test --test-isolation=none tests\app_navigation.test.mjs tests\workspaces.test.mjs
```

默认测试主要使用临时数据库、可控模型响应及引擎替身。真实 Desktop、模型服务和安装包验证是另外的显式操作；先检查[联调脚本与副作用说明](docs/ARCHITECTURE.md#真实环境验证)，不要把真实写入、收费请求或本机数据修改当成普通单元测试。

## 构建与交付

构建要求 Windows、64 位 Python、Inno Setup 6 和发布者已有的验证公钥：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -X utf8 -m scripts.build_desktop --version 2.0.4
```

同版本构建会覆盖对应本地产物；发布新改动前应统一版本并确认输出路径。构建脚本只需要公钥，**不要重新生成签发密钥来做普通升级**。只向客户分发安装包和使用说明，不分发源码、发码工具、签发私钥或用户数据。

安装版支持离线一机一码的永久／7 天试用授权；试用从签发时起算 604800 秒，安装、启动及使用过程中校验。源码入口用于开发，不要求授权。当前没有商业 Windows 代码签名；CD-KEY 签名不等于 SmartScreen 发布者签名，也不是不可绕过的防破解保证。

完整发码、私钥备份、静默部署和安装验证步骤见[授权与发布说明](docs/LICENSING.md)。
