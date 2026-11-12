# 技术架构与开发接手指南

本文面向接手开发的工程师与 agent，描述 **2026-11-12 当前源码实际实现**，不是未来功能设计。操作步骤见[使用说明](USER_GUIDE.md)，部署见[授权与发布说明](LICENSING.md)，测试历史见[验证记录](VALIDATION.md)。本文件是阅读入口，不会自动注入应用的模型请求，也不替代源码中的运行时提示词。

## 章节导航

- [运行拓扑](#1-运行拓扑与职责) · [实体与存储](#2-实体身份与持久化) · [连接与同步](#3-power-bi-连接重连与同步)
- [语义与字段证据](#4-语义层与真实字段证据) · [生成、验证与写入](#5-生成验证与写入链路)
- [流式、线程与页面状态](#6-流式传输线程与前端状态) · [模型适配与提示词](#7-模型配置提示词与诊断)
- [API](#8-主要-api-索引) · [限制与性能](#9-重要限制与性能边界) · [数据与授权](#10-图片数据边界与授权)
- [构建与版本](#11-构建版本与运行证据) · [修改与回归测试](#12-修改任务与回归测试) · [未实现能力](#13-尚未实现的能力)

## 接手时先记住

1. 这是 **Windows 本地单进程桌面应用**：pywebview + FastAPI + 原生 JavaScript + SQLite，不是云端多租户服务。
2. “Power BI 模型”“AI 服务配置”“聊天会话”“一次生成任务”是四种不同实体，不能混用它们的 ID。
3. 当前 AI 接口主要输出文本或 JSON 计划，**不是原生 Function Calling / MCP 自主工具循环**。工具选择、查询、重试和写入边界由后端代码控制。
4. 当前结构、语义候选、用户确认和真实查询结果是不同等级的依据，不能互相冒充。
5. **连接另一个报表可以进入新对话；同步当前报表必须保留原对话。**
6. 切换页面不停止生成；HTTP 查看器断开也不等于取消生成；关闭整个后端才会停止内存任务。
7. 生成和验证不写入物理模型；写入只走单独的用户确认接口。Power Query M 没有外部写回路径。
8. 修改源码不等于更新已有安装包。当前 API 与最新安装包版本为 2.0.4，包含计算列、业务词典扩展、分批分析及按时间和已有证据继续字段查值；具体构建证据与交付范围见验证记录。

建议阅读顺序：

1. [desktop.py](../desktop.py) → [backend/app.py](../backend/app.py)：启动、对象装配、API 边界。
2. [frontend/app.js](../frontend/app.js) → [frontend/workspaces.js](../frontend/workspaces.js)：页面、会话与任务归属。
3. [backend/chat.py](../backend/chat.py) → [backend/analysis.py](../backend/analysis.py)：DAX/M 与数据分析的不同执行链。
4. [backend/semantics.py](../backend/semantics.py) → [backend/semantic_diff.py](../backend/semantic_diff.py)：语义就绪、复用和增量失效。
5. [backend/validation.py](../backend/validation.py) → [backend/engine/](../backend/engine/)：真实引擎适配及生成／验证／写入的隔离。
6. 本文的[修改任务与回归测试](#12-修改任务与回归测试)：选择覆盖行为的测试，而非只改一个看起来相关的文件。

## 1. 运行拓扑与职责

```text
desktop.py
  ├─ 原生窗口：pywebview / Edge WebView2
  │    └─ frontend/index.html + ES Modules
  │         └─ 同源 HTTP JSON / NDJSON
  └─ Uvicorn / FastAPI（127.0.0.1，本机后端）
       ├─ SettingsService ── OpenAI 兼容 / Anthropic ── 配置的模型服务
       ├─ PowerBIService ── pythonnet / ADOMD ── Desktop 本地分析引擎
       │                  └─ TOM ── 用户确认后的模型写入
       ├─ SemanticService ── 本地事实、增量差异、AI 候选业务定义
       ├─ ChatService / AnalysisService ── 业务流程编排
       └─ Database ── SQLite（配置、会话、语义、查询证据、审计）
```

### 启动与退出

[desktop.py](../desktop.py)解析 `--browser`、`--port`、`--no-open`，确定数据目录和日志，调用 `create_app()`，再绑定 loopback socket。默认动态端口；原生模式在后台线程运行 Uvicorn，主线程启动 WebView2。窗口关闭后请求服务器退出。

[create_app 所在模块](../backend/app.py)按以下顺序装配：

1. 检查安装版授权；源码模式返回开发授权状态。
2. 创建 `Database`、`SettingsService`、`PowerBIService`。
3. 创建 `SemanticService` 并挂到 `powerbi.semantics`。
4. 创建 `ChatService`，它持有 `AnalysisService` 与 `ImageStore`。
5. 注册 API、中间件、`/assets` 静态目录和首页；启动时清理过期待发送图片。

部分同步和阻塞调用由 FastAPI 线程池承担，流式生成另有 Python 工作线程。锁、任务表和取消令牌都在进程内，**当前不支持简单增加多个 Uvicorn worker 来扩容**，否则同一会话的锁和任务状态不再共享。

### 技术栈

| 层 | 现有选择 | 维护入口 |
| --- | --- | --- |
| 桌面壳 | pywebview，Windows Edge Chromium | [desktop.py](../desktop.py)、[桌面依赖](../requirements-desktop.txt) |
| HTTP / 数据校验 | FastAPI、Starlette、Pydantic、Uvicorn | [app.py](../backend/app.py)、[schemas.py](../backend/schemas.py) |
| 前端 | 原生 ES Modules、CSS；无 React/Vue、无生产打包步骤 | [frontend/](../frontend/)、[package.json](../package.json) |
| 文本渲染 | 本地 Markdown-it、DOM 增量更新 | [markdown.js](../frontend/markdown.js)、[messages.js](../frontend/messages.js) |
| AI | OpenAI / Anthropic SDK、httpx、系统证书信任 | [engine/llm/](../backend/engine/llm/)、[net.py](../backend/engine/net.py) |
| Desktop 引擎 | pythonnet、ADOMD、TOM | [engine/context/](../backend/engine/context/)、[engine/dax/live_eval.py](../backend/engine/dax/live_eval.py) |
| 持久化 / 图片 / 加密 | SQLite、Pillow、Fernet、Ed25519 | [database.py](../backend/database.py)、[images.py](../backend/images.py)、[licensing.py](../backend/licensing.py) |
| 发布 | PyInstaller + Inno Setup | [packaging/](../packaging/)、[build_desktop.py](../scripts/build_desktop.py) |

## 2. 实体、身份与持久化

### 不要混淆的 ID

| 实体 | 标识 | 生命周期与用途 |
| --- | --- | --- |
| AI 配置 | `llm_profile_id` / `profile_id` | 供应商、模型 ID、Key、强度和等待参数；不是 Power BI 模型 |
| Power BI 模型记录 | `model_id` | 一份结构上下文及报表关联；连接或同步可创建新记录 |
| 报表身份 | 名称、可取得的路径、引擎 PID/端口 | 跨 Desktop 重开定位同一报表；端口本身不是稳定身份 |
| 结构指纹 | `fingerprint` / 规范化结构比较 | 用于结构和语义一致性检查，不是业务数据版本号 |
| 语义对象 | `object_id(kind, table, name)` | 表／列／度量的哈希 ID；改名会改变 ID，按删除与新增处理 |
| 会话 | `conversation_id` | 固定工作空间模式，关联当前模型；同步更新关联而不换会话 ID |
| 消息 / 产物 | `message_id` / `artifact_id` | 保存原回复和解析代码；写入以产物为操作对象 |
| 本次生成 | `generation_id` | 区分同一会话的不同请求，防止迟到取消误伤新任务 |
| 前端页面 | `view.key`、`runId` | 尚无持久化会话时也存在；异步回调绑定原页面和原任务 |

[ModelContext](../backend/engine/context/base.py)包含表列、关系、度量、日期表、计算表、表级 M、共享表达式和查询目录。它是提示词、字段检查和验证的结构事实源，不保存全量业务表。

模型记录并非绝对不可变：普通连接／同步通常创建新记录，`update_after_write()` 会刷新当前记录的结构和指纹。不能把 `model_id` 当成永不变化的数据内容哈希。

### SQLite 表关系

[database.py](../backend/database.py)使用 WAL；每次操作独立连接，开启外键，成功提交、异常回滚，连接不跨线程复用。当前迁移以 `CREATE ... IF NOT EXISTS`、列存在性检查及服务级配置迁移为主；`PRAGMA user_version=1` 不是应用发布版本，也不是完整迁移框架。

| 表组 | 表 | 保存内容 |
| --- | --- | --- |
| AI 配置 | `settings`、`llm_profiles` | 当前配置镜像、独立配置与加密 Key |
| 模型与语义 | `models`、`model_reports`、`model_semantics` | 结构、报表身份、语义 payload |
| 对话 | `conversations`、`messages` | 模式、当前模型关联、原文和状态 |
| 图片 | `message_images` | 规范化图片 BLOB；未发送时 `message_id=NULL` |
| 代码与审计 | `artifacts`、`audit` | 代码种类、公式、目标、验证、应用时间及写入审计 |
| 查询依据 | `analysis_queries`、`field_observations` | 每次业务查询／字段检查的结果、错误与截断信息 |
| 口径确认 | `message_clarifications`、`conversation_bindings` | 确认卡、答复状态、本会话确认的编号归属 |
| 值证据缓存 | `field_value_profiles`、`model_value_lookups` | 有结构、时间和完整性信息的取值证据，不是全量数据仓库 |
| 生成诊断 | `llm_generations` | 阶段、尝试次数、耗时、用量、结束原因，不含思考正文 |
| 同步历史 | `conversation_model_syncs` | 原／新模型 ID、同步时间、旧消息 rowid 边界 |

会话下的消息及消息子表多采用级联删除；模型与会话不是同一生命周期。删除聊天不会撤销已写入 Desktop 的对象。`audit` 的产物 ID 不是级联外键，不要假设删除会话会清除全部审计。

### 持久化与内存边界

- SQLite：已发送消息、附件、语义、查询证据、配置和同步记录，重启可恢复。
- 前端内存：未发送文字、校准条件、待提交确认、附件选择、滚动位置、当前页面。
- 后端内存：`live_ids`、会话锁、取消令牌、进度、重连状态、语义生成线程。
- 后端重启不会续跑原线程；历史中失去对应任务的 `streaming` 消息会在读取时标记中断。语义 `building` 若没有对应 job，也会转为可继续处理的错误状态。

## 3. Power BI 连接、重连与同步

主要入口：[powerbi.py](../backend/powerbi.py)、[reports.py](../backend/reports.py)、[reconnection.py](../backend/reconnection.py)、[live_source.py](../backend/engine/context/live_source.py)。

### 连接与读取

1. `find_instances()` 根据 Desktop 分析引擎进程和监听端口发现候选实例。
2. `reports_for_instances()` 尝试关联报表窗口名称与路径；无法取名时允许用户明确填写。
3. `LiveDesktopSource.load()` 用 ADOMD / DMV 读取结构，构建 `ModelContext`；扩展描述、格式、来源等由语义元数据读取补充。
4. 保存模型和报表记录，执行语义本地对齐／历史复用；**连接本身不会调用 AI 梳理语义**。

ADOMD 负责读与查询；TOM 负责明确授权的写入。[amo.py](../backend/engine/context/amo.py)管理 TOM 组件加载和本地缓存，首次可能从官方 NuGet 下载。不要把缺 DLL 或断连降级成“已经实跑”。

### 重连与结构一致性

同 PID/端口优先定位原引擎；引擎重开后使用报表身份，匹配不唯一返回 `ambiguous`，不能随机选实例。取得实例后比较规范化结构，忽略无关顺序和取值数量提示，不忽略公式、字段类型等真实变化。

主要连接状态：`checking`、`connected`、`reconnecting`、`disconnected`、`changed`、`ambiguous`，示例为 `demo`。`live_ids` 只是当前进程的状态缓存，任务开始前仍走 `require_live()`。

前端连接监控约每 3 秒更新页面状态；后端正常核对间隔 20 秒，失败后按 2/5/10/20/30 秒退避。后台核对非阻塞获取 Desktop 锁，已有查询或写入优先。引擎身份改变后保留语义、清理旧值证据缓存。

### 当前对话同步：不可回归的链路

```text
frontend/app.js: syncCurrentModel()
  ├─ 已有会话 → POST /api/conversations/{id}/refresh-model
  │    └─ ChatService.refresh_model()
  │         → 获取本会话锁和 Desktop 锁
  │         → 同报表新模型快照 + 增量语义
  │         → 原 conversations 行更新 model_id
  │         → 保存同步记录与旧消息边界
  │         → 清理失效的编号绑定
  │         → 返回同一个 conversation_id
  └─ 尚无会话 → POST /api/models/{id}/refresh，只更新当前页面模型
```

前端更新原 `view`，保留草稿、图片和消息；同步期间导航后，迟到结果不能激活或覆盖另一个页面。同步失败不改原会话关联，finally 释放操作状态。

旧助手消息的 `model_stale` 根据同步边界计算，不改写历史正文；旧口径卡不能继续提交。新请求注入同步提示，查询当前数据时重新取数。历史代码重新验证／写入也以**会话当前模型**为准，而非机械使用旧产物的模型 ID。

不要将同步重新接回 `useModel()`：该函数用于选择报表，会调用 `workspace.newChat()`，正是旧问题的来源。

## 4. 语义层与真实字段证据

主要入口：[semantics.py](../backend/semantics.py)、[semantic_diff.py](../backend/semantic_diff.py)、[fields.py](../backend/fields.py)、[values.py](../backend/values.py)、[clarifications.py](../backend/clarifications.py)。

### 语义对象和状态

每个对象分开保存：

- `facts`：名称、类型、关系、公式、Description、显示格式、来源等模型事实。
- `business_name / meaning / role / unit / aliases / usage_notes`：业务定义及适用边界。
- `state`：`unresolved`（未知或需更新）、`candidate`（候选）、`confirmed`（用户确认）。
- `source / reviewed_at`，以及变化原因和 `previous_definition`。

词典级 `status` 为 `inventory / building / ready / error`，还有 `metadata_status`、`coverage`、`pending_ids`、复用来源和变化摘要。

**`ready` 不代表所有定义已由用户确认；`can_answer` 也不等于 `status == ready`。** 已有对象候选、确认、历史定义，或可用业务总览／词条时，DAX／分析可继续用当前结构和可用语义回答；未知口径按需澄清。首次完全没有解释时才要求先建立词典。M 模式没有此语义前置条件。

### 业务总览、跨对象术语和分析约定

[business_dictionary.py](../backend/business_dictionary.py) 在原 `model_semantics.payload` 内维护 `business_dictionary`，不新增第二份模型事实表。`version=1`；首次读取旧记录补空业务词典和对象 `usage_notes`。`overview` 保存业务全貌；`items` 的 `kind` 为 `term / convention`，带别名、说明、适用范围、对象引用、可选列筛选、业务依据、候选／确认／待复核状态和审阅时间。全局 `revision` 用于编辑冲突检查，`deleted_ids` 防止历史复用恢复已删词条。

API 位于 `/api/models/{model_id}/semantics/business`：`PUT /overview`、`POST /items`、`PUT/DELETE /items/{id}`、`POST /suggest`。引用必须是当前模型真实对象；筛选只能引用列，支持 `eq / in`，值按字符串保存。业务规则不修改原生关系或 DAX。新增词条上限 500；单词条关联最多 20 个对象、10 组筛选。

`_align()` 使用本次实际变更集合使引用受影响对象的业务记录失效，避免累计 UI 变更清单反复撤销刚确认的定义；业务总览在模型变化后保守转为待复核。历史复用深拷贝业务记录，目标词典的手动修改和删除优先。同一报表跨运行复用遵循现有报表身份匹配，不因结构相似就关联其他业务报表。

“生成业务候选”使用独立后台任务及诊断阶段 `business_dictionary`，输入模型对象约 45,000 字符，轮流选择表、度量、列并声明遗漏数；JSON 结果最多八项，强制保持候选，不接受虚构对象或 AI 生成的筛选值。任务写回前比较模型 fingerprint 和词典 revision，冲突时丢弃本次建议，已有记录保持不变。用户编辑不被后台候选覆盖；服务重启后中断任务显式标错，可重试。

业务术语通过临时 `search_aliases` 参与对象检索，不写回对象业务别名，也不改变对象确认状态。总览提供全局业务背景，相关术语和范围命中的约定按预算装入 `business_dictionary` 提示词块；失效记录只输出待复核标记，省略旧含义及筛选值。约定可用于用户未指定的条件，显式要求和 DAX 优先。未命中词典的问题仍可组合真实模型对象查询。分析总结复用规划时的语义快照，实际数值仍来自 `evidence`。这些约束通过上下文指导生成，不是一个能证明业务正确性的查询编译器。

前端 [business_dictionary.js](../frontend/business_dictionary.js) 提供表单和展示组件，[semantics.js](../frontend/semantics.js) 管理业务总览、业务词条、对象词典三个当前模型视图，以及请求和后台轮询。历史词典通过独立的“词典管理”动作打开，不属于模型视图标签。管理页显示时隐藏并暂停模型工作台；返回后恢复原视图、筛选和位置，再读取当前关联状态。业务编辑提交携带打开编辑时的 revision，冲突保留输入并提示返回刷新。

### 历史词典目录、差异预览和删除

[dictionary_library.py](../backend/dictionary_library.py) 复用 `model_semantics` 保存的模型版本，不新建重复的定义存储。目录接口 `GET /api/semantic-dictionaries?current_model_id=...` 按保存版本返回名称、路径、时间、覆盖、业务记录数量、同名对象数、结构是否一致和忙碌状态；界面按报表路径／名称／已记录引擎身份归组。只包含有业务解释或可复核历史的版本，不把全空初始清单当作已建词典。

`PUT /api/semantic-dictionaries/{id}/name` 接受 `{name, updated_at}`。名称为 1～160 字单行文本，保存到 profile 的 `dictionary_name / dictionary_name_source / dictionary_name_edited_at`；不改变报表身份、原生对象或业务定义。更新以时间戳进行 SQL 条件检查，梳理／生成期间拒绝命名。目录和预览优先显示自定义名称，并单独返回 `report_name`。连接／同步复用时继承名称，目标显式命名优先；同一报表的用户命名沿后续版本保留。名称不参与报表身份匹配。

`dictionary_content_updated_at` 记录词典内容版本时间；常规 `_store()` 更新该时间，纯重命名保留原值。目录返回 `saved_at` 用于展示内容保存时间，`updated_at` 继续作为包括名称编辑的并发版本令牌。`_history()` 按内容时间选择来源，防止命名旧版本后自动复用旧业务定义。旧记录缺少内容时间时回退到原保存时间。

管理页由 [dictionaries.css](../frontend/dictionaries.css) 独立样式控制：默认展示当前／最新一行，其他版本收起；名称、对象／业务记录统计、保存时间及操作分列，窄屏重新排布。日期固定按 Asia/Shanghai 格式化到分钟，原始时间戳仅用于并发令牌及时间属性。重命名使用行内表单，冲突保留草稿；刷新后更新版本令牌，再次保存只修改名称。路径、名称、错误和差异文本均转义显示。

`GET /api/models/{id}/semantics/reuse-preview?source_model_id=...` 在深拷贝上调用与实际复用一致的对齐过程，返回对象变化明细及复用后的业务待复核数量，不保存预览结果。`POST .../semantics/reuse` 允许不同结构的历史词典，仍保留目标已审阅定义；旧定义失效规则不放宽。传入预览中的 `source_updated_at` 时，来源版本变化则返回 409。连接后自动关联继续按已有报表身份策略进行，不按结构相同就自动共享其他命名报表的定义。

比较 M／计算表来源使用来源词典的 `source_snapshot`；这能覆盖模型被原地写入更新后、词典尚未再次对齐的情况。不能直接把已更新的来源模型上下文当成词典当年的依据。

`dictionary_notice` 同时反映对象解释与业务记录的待更新状态，附加在完整词典和模型语义摘要中。业务记录仍失效时，即使对象语义覆盖已经完成也继续提示；完整扩展元数据未核对时标为 `unverified`。前端 [dictionaries.js](../frontend/dictionaries.js) 提供独立的历史目录、搜索、预览、关联和逐版本删除；入口位于连接窗口和模型工作台右上方“词典管理”。`showModal(...,{wide:true})` 仅对模型语义和词典管理启用加宽样式，其他弹窗恢复默认宽度；嵌套的字段预览沿用当前窗口宽度。

`DELETE /api/semantic-dictionaries/{id}?current_model_id=...&updated_at=...` 仅删除对应 `model_semantics` 行，不删除模型、消息或其他版本。按当前目标、语义／业务候选后台任务和 Chat 会话锁阻止删除正在使用的版本，关联到忙碌目标也被拒绝；客户端携带保存时间，并在 SQL DELETE 中再次限定版本防止并发更新被误删。已关联副本独立保存，删除来源后仍可使用；旧会话重新生成时若其自身词典被删除，则需重新建立或关联。目录和关联操作不调用外部模型。

### 生成与增量复用

`on_connect()` 本地读取扩展元数据并寻找同报表历史词典。跨运行复用依据报表身份；旧无身份词典仅在候选可唯一确定时关联。歧义需要用户选择，不按相似表名猜测。

`reconcile()` 比较对象事实及可识别的 M/DAX 依赖：

- 未变化对象保留原解释和确认状态。
- 新增／变更／依赖受影响对象回到 `unresolved`；旧定义留作参考，不能继续冒充有效确认。
- 删除对象退出当前词典；重命名按删除和新增，不自动移植业务确认。
- M 动态引用和关系传播采用保守处理；这不是完整 DAX/M 编译器或全量运行时依赖图。

用户显式开始梳理后，后台只处理待更新对象，每批 20 项；AI 返回 JSON，校验 ID 集合和字段形状，格式失败最多补一次请求。逐批持久化，重试从剩余对象继续。`expected fingerprint` 防止旧任务覆盖新结构。

### 按问题选择语义

`SemanticService.prompt(model_id, question, history=..., bindings=...)` 通过 [semantic_retrieval.py](../backend/semantic_retrieval.py) 在本地选择本轮定义，不增加模型请求或向量服务。DAX/M 生成和分析规划均传入当前问题、原有预算内的历史消息及会话编号映射。

- 优先匹配明确的表列引用、用户确认的编号映射、对象名、业务名和别名；再用中文二字片段、英文词及描述中的词项重合补充召回。已确认状态仅用于相关性相同的排序，不会让无关定义挤掉明确命中对象。
- 当前明确指定新指标时切换主题；维度或短句追问最多回看最近 4 条用户消息，在最近明确指标处停止。历史助手回复和 `previous_definition` 不作为检索依据，检索不会将候选定义提升为已确认口径。
- 最多选 32 个匹配起点，补充可静态识别的多层 DAX 依赖、所属表、连接命中表的最短关系路径和日期上下文。同长路径以及路径上的平行活动／非活动关系均保留，不代替用户选择日期角色。关系检索仅说明结构连接，不能证明实际筛选传播有效。
- 条目序列化预算为 50000 字符；先装入匹配对象及依赖，再装入辅助上下文。单项放不下时继续尝试后续条目。`retrieval` 记录模式、匹配数、已用预算、遗漏相关对象数、最多 20 个缺失对象和最多 64 条关系及其遗漏数量。
- 无匹配时按表轮流选取概览，避免只覆盖排序靠前的大表；明确标为 `fallback`，无问题文本时标为 `overview`。完整模型结构仍由原路径注入，语义未选中不等于对象不存在。

这是可检查的词项检索，不是 embedding 相似度搜索；没有共同词项或已维护别名的同义表达可能无法命中。依赖识别复用现有静态解析能力，动态 DAX/M 语义不属于完整覆盖。

### 查值不等于确认

`FieldService.preview()` 执行受限的统计／去重值／样例查询，返回范围、时间、错误和截断标记；不继承报表视觉筛选。

`ValueEvidence.locate()` 对带前导零的编号作精确匹配，最多检查 96 个文本或未知类型字段，分批取证。匹配值存在不代表用户希望采用该字段筛选；多个归属或取证不完整时通过确认卡澄清。

编号映射保存于原会话，不会自动提升为全局语义。旧助手自加条件不能当作用户确认。部分问题可由本地证据直接解释，不需要调用模型。

## 5. 生成、验证与写入链路

### 5.1 共用入口

```text
前端 send → generation.send(view, request)
  → 必要时创建 conversation
  → POST /api/conversations/{id}/messages
  → ChatService.begin()
      → 图片、模型、查询、校准和连接校验
      → 会话锁 + generation_id + CancellationToken
      → 已同步旧口径卡校验
      → DAX/分析的语义与编号取证
      → 本地解释/确认，或选择 provider
      → DAX/M: ChatService.stream()
      → 分析: AnalysisService.stream()
  → BackgroundStream → NDJSON → 原 view
```

前端复制请求中的筛选、附件和模型配置 ID，避免切页后读取另一页面的输入。后端创建 provider 后复用于该流程；配置修改不改造已创建的 provider。若要严格讨论“点击发送瞬间的全部配置快照”，还需注意真实语义取证发生在 provider 创建前，不要把配置 ID 与不可变配置版本混为一谈。

### 5.2 DAX 与 M

DAX 真正的系统提示入口是 [build_chat_system_prompt 所在模块](../backend/engine/dax/prompts.py)，由 [ChatService.stream](../backend/chat.py)调用，并追加中文约束、确认规则、图片规则、同步提示、语义和字段证据。它包含上下文转换、迭代器、复用基础度量、筛选修饰符、日期表等规则。

M 使用 [M 提示词](../backend/engine/mquery/prompts.py)和 `serialize_query_for_prompt()`，提供所选查询、输出字段及共享查询／参数。

提示词模块中的其他 `build_*` 是独立辅助构造器，**不能仅因函数存在就认定桌面主流程调用了它**。旧模块注释提到的开发 skill／旧项目路径也不是运行时依赖；实际生效依据 import 和调用链，不会自动加载外部 agent 技能。

```text
AI 回复
  → candidates() / parse_dax_objects()
  → 静态引用、结构检查
  → Desktop 验证（仅 DAX）
  → 有失败：把真实验证结果送回模型，最多再修复两轮
  → 保存原文、修订和最后候选产物
```

当前产物种类：

| `kind` | 识别与验证 | 写入能力 |
| --- | --- | --- |
| `dax_measure` | 独立 `dax` 块；查询作用域 `DEFINE MEASURE` 验证，支持基础／派生组合 | 修改名称、存放表；同表覆盖需明确允许 |
| `dax_table` | 表表达式分类；受限 `EVALUATE` 验证 | 仅新建计算表 |
| `dax_column` | `dax-column` 或完整 `'表'[列] = ...`；必须明确所属表 | 仅新建计算列，不能覆盖现有字段 |
| `m_script` | M 围栏；`let/in`、括号及引用等静态检查 | 不外部写入；复制后由用户应用 |

[validation.py](../backend/validation.py)是共享验证入口；[generate.py](../backend/engine/dax/generate.py)解析代码块；[artifact.py](../backend/engine/dax/artifact.py)与[M 检查器](../backend/engine/mquery/artifact.py)做静态校验。

计算列不能冒充度量来验证。`evaluate_column_expr()` 使用 `SELECTCOLUMNS(SAMPLE(...))` 保留所属表行上下文，最多 50 行、20 秒，明确显示抽样和空表限制。尚未物理写入的其他计算列不能当作已有字段引用。

`ok` 表示本次验证结果，`run_verified` 表示是否到达真实引擎验证；二者必须一起读。静态检查不是完整语言验证，实跑或抽样也不证明业务口径正确。

校准的 `points` 保留数组协议，但当前最多一个目标值：最多 10 个筛选条件取交集，比较最后一个相关度量的结果与 `expected/tolerance`。计算列不参与度量校准。

### 5.3 数据分析

[analysis.py](../backend/analysis.py)不是普通 DAX 聊天的别名，而是独立流程：

1. AI 返回 `AnalysisPlan` JSON：`scope / clarification / field_checks / queries / needs_more / remaining_questions / assessment`。根据问题复杂度先规划必要的一批查询，简单问题优先一条聚合。
2. 如需字段证据，读取真实取值，再请求规划；不设固定两轮限制。按剩余时间及已观察的查值／规划耗时决定是否继续，整轮最多 12 项实际检查、同一字段最多 3 组不同搜索，跨业务查询批次共享。相同检查复用已存结果；未筛选且未截断的全字段取值可覆盖新搜索，完整搜索结果只覆盖原关键词及其子集，截断样例不能冒充完整集合。预算与复用逻辑见 [field_discovery.py](../backend/field_discovery.py) 和 [analysis_budget.py](../backend/analysis_budget.py)。
   到达上限、时间不足或重复请求时，最多再让模型根据已有证据收尾一次；时间允许且依据足够仍可返回业务查询。若仍请求查值，则从同一真实字段的未截断值生成候选确认卡；没有可靠候选时明确要求补充定义，不把失败当作无数据。观察记录保留，规划上下文替换旧查值快照，避免多轮重复累积同一取值列表。
3. 口径仍不清楚时保存确认卡，不编造查询。
4. `readonly_query()` 检查只读形状和禁用语句；每条只允许一个 `EVALUATE`。
5. 在 Desktop 顺序执行本批查询，立即保存成功或失败记录，以及所属 `batch` 和口径 `scope`。可识别的连接中断最多重连重试一次；失败查询可再请求一次代码修复。
6. 计划声明需要继续、存在未解决问题，或本批有部分失败／结果截断时，检查时间和数量额度，再以 `analysis_continue` 阶段评估本轮真实 evidence。证据足够则停止，不足则只补查下一批；续查保持原口径，需改变口径时返回确认卡。整批失败、重复查询、连接变化或查询等待超时会停止补查，已有成功结果仍可总结。
7. 汇总阶段只依据本轮成功 evidence，引用 `[Q1]` 等来源，编号跨批次连续；停止原因、未解决问题及未成功查询由应用补充说明。无成功结果时直接说明无法分析。
8. 总结没有任何正文且结束原因可重试时，复用相同结果和图片最多重试一次，并在支持时降为低强度；不重新执行查询。

规划、字段取值、查询、修复和总结共享分析流程的 deadline。[analysis_budget.py](../backend/analysis_budget.py) 预留 `min(180 秒, 总时限 × 25%)` 给总结；所有查询阶段的生成和等待均受扣除预留后的时间约束。下一条查询／下一轮规划还须通过近期耗时估计（保守取较慢观察值并留 20% 余量）的准入检查。总结可以使用全部剩余时间，预留不保证上游模型一定完成输出。进入分析前的共用语义取证另有等待限制。DAX/M 则是每次生成调用应用时限，不能把两种模式的超时范围混为一谈。

每批最多 4 条，单轮最多 12 条新业务查询、4 批、18 次应用查询调用（含 DAX 修复重试，底层连接恢复重试仍单独按原策略处理）。字段取值不占业务查询条数，但共享查询阶段时间。查询去重忽略代码空白、注释及非引用代码大小写，保留字符串值和引用标识符；不会把不同筛选字符串合并。修复也不能重复已执行或本批待执行的相同 DAX。

续查上下文按查询均分约 48,000 字符预算，每份结果最多 16 行；总结使用约 96,000 字符预算。只提供同一 Q 编号的最新执行尝试，并标记 `context_truncated / shown_rows / query_text_truncated`；单元格不会被裁成另一个值。此裁剪仅影响生成上下文，数据库、依据卡与 CSV 保留原结果及所有尝试。后续规划格式错误或修复超时不会丢弃先前成功结果；取消信号仍直接终止，不触发新总结。

空结果不等于零；TopN、样例和截断记录不能支持总体总额。查询结果按模型当前可读范围返回，不等于 Desktop 视觉对象的数据。

### 5.4 唯一受控写入入口

[app.py](../backend/app.py)中的 `POST /api/artifacts/{id}/apply`：

1. 检查产物存在、类型支持，且原验证 `ok && run_verified`。
2. 取得产物所属会话的**当前模型**，重新核对真实连接与结构。
3. 按用户最终名称和目标表检查重名、覆盖许可，再验证及校准。
4. 调用 [LiveDesktopWriter](../backend/engine/context/live_writer.py)。
5. 保存写入审计；仅成功后更新实际名称、目标、应用时间，刷新本地模型结构。

度量、计算表、计算列的规则不能合并成无条件 `overwrite`。计算列用 TOM `CalculatedColumn`，`Calculate` 请求在同一次 `SaveChanges` 中提交，失败尝试丢弃本地未提交变更。物理写入成功但随后读取失败时，要提示“已写入，需同步”，不能假报整体未写入并自动重试创建。

没有 `write_m_partition`；禁止把 M 回写当作普通模型对象写入。成功修改内存模型后，用户仍需在 Desktop 保存报表。

## 6. 流式传输、线程与前端状态

### 传输分为两段

模型服务的流式协议由 SDK 消费；应用向浏览器发送的是 **`application/x-ndjson`**，不是 EventSource SSE。

| 应用事件 | 含义 |
| --- | --- |
| `start` / `model` | 绑定助手消息和实际模型 |
| `status` / `heartbeat` | 阶段文字与保活 |
| `delta` | 追加正文 |
| `artifacts` / `analysis` / `field_previews` | 更新结构化代码、查询、字段证据 |
| `clarification` | 单项口径确认卡 |
| `done` / `error` / `cancelled` | 完成、失败、中止 |
| `background` | 当前查看器脱离流，改用状态与消息轮询 |

provider 内部还会产生 `activity`、`metadata`，分别用于上游活动和诊断，不作为正文展示。推理内容只报告“收到思考”与用量，不发送到 UI、不持久保存。

[streaming.py](../backend/streaming.py)提供三个不同职责：

- `completion_events()`：工作线程消费同步 SDK，经有界队列转发，管理空闲／总时限和诊断。
- `task_events()`：为阻塞只读任务提供等待上限与心跳；并非外部任务队列。
- `BackgroundStream`：在独立线程消费完整业务生成器，前端断开只停止向该查看器发布，不停止执行；缓冲溢出时提示转后台。

`copy_context()` 传播取消和授权检查到工作线程，不能在增加线程时丢掉它。没有 Celery/Redis 或进程退出后可续跑的持久任务系统。

### 前端归属与增量渲染

[workspaces.js](../frontend/workspaces.js)管理页面和生成；[app.js](../frontend/app.js)负责装配、导航、模型同步及交互：

- `view` 独立持有模型、会话、草稿、图片、校准、消息、滚动位置与 `busy/syncing/loading`。
- 创建会话前就将任务登记到原 view，不能 await 后再从全局当前页面取归属。
- 每个请求复制输入；同一会话不重复发送，不同页面可并行等待模型响应。
- 中断恢复通过 `/messages` 和 `/generation`，不重新 POST 原问题；合并快照时不让短的旧快照覆盖屏幕上更长的流式文本。
- 后台轮询目前有固定上限：普通模式 10 分钟、分析 20 分钟；不同于可配置的后端生成时限。轮询退出不等于后端已经停止，不能声称任意长请求都会被前端无限追踪。
- [messages.js](../frontend/messages.js)按节点增量修补并通过 `requestAnimationFrame` 合并渲染；保留代码复制按钮、历史正文、图片和用户滚动位置。连接状态刷新不应重画整段聊天。
- [stream.js](../frontend/stream.js)处理跨网络块的 UTF-8 和 NDJSON，检查终止事件，避免把无结束标记的断流当成功。

### 锁与中止

- `ChatService.locks[conversation_id]`：同一会话生成／同步互斥；`mutex` 保护运行任务登记。
- `PowerBIService.lock`：进程内 `RLock`，串行化关键 Desktop 读写操作；不同会话的 LLM 等待可并行，但不意味着引擎查询无限并发。
- `SemanticService` 的锁、job 和指纹检查保护同一模型语义任务与增量更新。
- [CancellationToken](../backend/cancellation.py)绑定一次生成，使用事件与关闭回调。取消接口必须携带 `generation_id`；不能只按会话 ID 取消。
- 中止触发模型流／客户端关闭，并对活动 ADOMD 命令注册取消；后续查询、修复和总结不再继续。上游算力释放和原生命令退出速度取决于服务端／驱动。
- 切页、关闭 HTTP reader 和主动中止是三种不同操作；不要用一个 `AbortController` 代替后端取消语义。

## 7. 模型配置、提示词与诊断

[SettingsService](../backend/settings.py)管理每模型独立配置，创建 provider 时应用输出额度、等待和 `max_retries=0`，避免 SDK 暗中重试绕过应用时限。空白 Key 只保留当前条目的 Key，不能借用其他条目。

[reasoning.py](../backend/engine/llm/reasoning.py)是参数匹配的单一来源：检查协议、域名、模型 ID／版本，返回 `effort / switch / unsupported`。前端卡片根据后端公开的能力信息展示，不维护另一套供应商判断。

- 原生强度：`reasoning_effort=low/high`。
- 开关型：按接口选择 `enable_thinking`、`thinking.type` 或 `chat_template_kwargs`；低档为标准回复。
- 固定或未确认版本：显示不可调，不进行能力探测、偷偷改模型或收费重试。
- 新旧未设强度配置默认低；已保存的选择不在重启时重置。兼容字段 `enable_thinking` 仍存在，不等于唯一实际控制参数。
- `OpenAICompatProvider` 和 `ClaudeProvider` 都提供同步及流式能力；扩展适配时必须检查两个路径。

主要提示词不在 agent 配置文件中：

| 流程 | 实际位置 |
| --- | --- |
| DAX 专业规则和对象格式 | [engine/dax/prompts.py](../backend/engine/dax/prompts.py) |
| M 规则 | [engine/mquery/prompts.py](../backend/engine/mquery/prompts.py) |
| 分析规划／总结 | [analysis.py](../backend/analysis.py) |
| 语义事实边界／分批解释 | [semantics.py](../backend/semantics.py) |
| 单项口径确认／答复验证 | [clarifications.py](../backend/clarifications.py) |
| 图片证据边界 | [images.py](../backend/images.py) |
| 会话同步与运行时附加约束 | [chat.py](../backend/chat.py) |

[GenerationReport](../backend/engine/llm/diagnostics.py)归一化 `finish_reason`、token 用量、request ID、思考活动、输出长度、耗时和异常类型；[generation.py](../backend/generation.py)按阶段／次数存入 `llm_generations` 并记录诊断。必须区分 `length`、空正文、仅思考、过滤、无结束标记、超时、中止；不能全部显示“生成成功”。

## 8. 主要 API 索引

以下路径都以 `/api` 开头；定义以 [app.py](../backend/app.py) 为准。运行后访问本机 `/api/docs` 可查看 OpenAPI，正式版同样受授权检查。

| 领域 | 主要路由 | 服务 |
| --- | --- | --- |
| 状态 | `GET /health` | 运行版本、平台与授权摘要 |
| AI 配置 | `GET/PUT /settings`，`POST /settings/test`；`/llm/profiles` 的列表、新建、编辑、删除、`activate`、`test`；`PATCH /llm/profiles/{id}/reasoning` | `SettingsService` |
| 模型 | `GET /desktop/instances`，`POST /models/connect`、`/models/demo`，`GET /models`、`/models/{id}`，`POST /models/{id}/refresh` | `PowerBIService` |
| 连接 | `GET /models/{id}/connection` | `DesktopReconnector` |
| 语义与查值 | `/models/{id}/semantics`，`semantics/build`、`semantics/reuse`、`semantics/{entry_id}`，`POST /models/{id}/fields/preview` | `SemanticService` / `FieldService` |
| 会话 | `/conversations`、`/conversations/{id}/messages`、`generation`、`cancel`、`refresh-model`、`export`；删除会话 | `ChatService` |
| 代码 | `POST /messages/{id}/validate`，`POST /artifacts/{id}/apply` | 验证／写入流程 |
| 图片 | `POST /images`，`GET/DELETE /images/{id}`，`POST /desktop/screenshot` | `ImageStore` / Windows 截图入口 |
| 查询导出 | `GET /analysis/{id}/csv` | 实际保留结果 |

流式开始前可用 HTTP 4xx 拒绝；开始之后错误通过事件传递并保存部分内容。前端不能仅凭 HTTP 200 判断生成成功。

## 9. 重要限制与性能边界

下面是当前实现值，不是建议随意放宽的配置。改动前定位常量和测试。

| 项目 | 当前值／行为 | 来源 |
| --- | --- | --- |
| 默认输出额度 | 16000 token；配置范围 256～131072，服务端仍可能有更小上限 | [schemas.py](../backend/schemas.py)、[diagnostics.py](../backend/engine/llm/diagnostics.py) |
| 等待 | 空闲默认 90 秒、范围 30～600；总时限默认 900 秒、范围 180～7200；心跳 2 秒 | [streaming.py](../backend/streaming.py)、[schemas.py](../backend/schemas.py) |
| DAX/M 历史 | 最多最近 20 条完整消息、60000 字符预算 | [chat.py](../backend/chat.py) |
| 分析历史 | 最多最近 16 条完整消息、40000 字符预算 | [analysis.py](../backend/analysis.py) |
| DAX/M 候选与修复 | 单次最多 20 个对象，初次生成后最多两轮修复 | [chat.py](../backend/chat.py) |
| 分析结果 | 每批最多 4 条；单轮最多 12 条、4 批、18 次应用执行尝试（含修复）；每条最多保留 200 行、30 列、24000 字节行数据 | [analysis.py](../backend/analysis.py)、[analysis_budget.py](../backend/analysis_budget.py) |
| 语义生成 | 每批 20 对象；本批返回格式失败最多再请求一次 | [semantics.py](../backend/semantics.py) |
| 注入语义 | 按问题匹配最多 32 个起点并扩展依赖；条目 50000 字符预算，附缺失对象与关系信息；无匹配时按表概览 | [semantic_retrieval.py](../backend/semantic_retrieval.py) |
| 校准 | 一个预期值，最多 10 个 AND 条件，仅度量值 | [schemas.py](../backend/schemas.py)、[validation.py](../backend/validation.py) |
| 计算列验证 | 最多 50 行、20 秒；抽样而非全量计算 | [live_eval.py](../backend/engine/dax/live_eval.py) |
| 编号检查 | 最多 96 个候选字段；分批，单命令最多 5 秒、单编号约 15 秒 | [values.py](../backend/values.py) |
| 图片 | 每消息最多 4 张，原图每张 ≤8 MB，≤2000 万像素；长边规范到 ≤2048，保存 ≤2 MB；请求最多 8 张历史／本轮图 | [images.py](../backend/images.py)、[schemas.py](../backend/schemas.py) |

界面心跳不延长上游空闲时限；思考帧算真实活动但不是答案。增大 `max_tokens` 或 timeout 不会让模型更快。结构指纹不证明数据新鲜；任何未来看板缓存都不能仅以结构未变为由长期复用业务数值。

## 10. 图片、数据边界与授权

### 数据与文件位置

数据目录由 [data_directory](../backend/app.py)决定：`PBI_DATA_DIR` 优先；源码默认根目录下 `data`，安装版默认 `%LOCALAPPDATA%\PowerBIAssistant`。

- `assistant.sqlite3` 保存用户数据，`secret.key` 是本地 Fernet 解密材料，二者必须成套备份。
- 先关闭应用再复制数据目录，避免遗漏活动 WAL；不以删除数据库解决迁移或解密问题。
- API Key 加密不等于整个数据库加密：对话、图片、语义和查询结果是本地可读数据，备份需按敏感资料处理。
- 图片先经 Pillow 校验、去除附带元数据并规范化；发送时才绑定消息。启动时清理超过两天的未发送图片。
- 查询结果、模型结构、M、语义、对话和图片可能被发给所配置的 AI 服务；原始 PBIX 不上传。选择模型服务是数据权限决策，不只是性能选择。

应用仅面向 loopback：Host 白名单、同源检查、JSON 要求、大小限制、CSP 和 `no-store` 限制浏览器跨站访问；它不是可直接暴露公网的多用户认证系统。

### 授权与线程传播

[licensing.py](../backend/licensing.py)实现机器码、Ed25519 校验、永久／试用授权和运行中时间检查。正式版通过冻结标记进入授权路径，`license.key` 位于可执行程序旁，改变 `PBI_DATA_DIR` 不改变授权位置。

`LicenseGuard` 在 API 操作及后台执行中检查试用到期，授权上下文通过 [cancellation.py](../backend/cancellation.py)和工作线程传播。浏览器预览参数不能绕过冻结安装版检查。试用从签发起 604800 秒，重装不续期；离线时钟／系统快照仍有固有限制。

发布私钥位于项目外，使用 Windows DPAPI 保护，仅发布者发码工具解密；应用和安装验证器只内嵌公钥。不要将 [license_admin.py](../scripts/license_admin.py)、真实密钥或用户数据加入冻结发布包。

## 11. 构建、版本与运行证据

[build_desktop.py](../scripts/build_desktop.py)校验 64 位 Windows、公钥和 Inno Setup，生成构建用 `_license_trust`，再调用 [powerbi.spec](../packaging/powerbi.spec)：

- `PowerBIAssistant`：桌面应用目录，收集前端资源和运行依赖。
- `LicenseVerifier.exe`：安装阶段用的验证器。
- [installer.iss](../packaging/installer.iss)：打成 per-user 安装包；固定 AppId 支持覆盖升级，静默安装也必须验码。

发布版本要同时核对后端 OpenAPI／health、构建默认值、安装器默认值和使用说明。`--version` 当前主要传给安装器，**不会自动改写后端版本字符串**。源码提交标签 `V-1.0.x` 与安装包 `2.0.x` 是现有的不同命名序列。

[.gitignore](../.gitignore)排除运行数据、构建目录、发行目录、日志和私钥备份等。其他路径下的 ZIP 不会因为包含安装包就自动被忽略；提交前检查范围，不把本地交付文件误当源码。

不要把“单元测试通过”“真实 Desktop 查询通过”“真实模型写入通过”“安装包已构建”混为一项。历史证据及边界按日期保存在 [VALIDATION.md](VALIDATION.md)；后续改源码不会更新已存在的 EXE。

## 12. 修改任务与回归测试

所有命令在项目根目录用 PowerShell 执行。日常先运行覆盖变更的最小集合；修改公共接口、持久化或状态机时再扩大回归范围。

| 修改内容 | 主要文件 | 优先测试（均在 [tests](../tests/)） |
| --- | --- | --- |
| 菜单、导航、异步归属、草稿 | [index.html](../frontend/index.html)、[app.js](../frontend/app.js)、[workspaces.js](../frontend/workspaces.js) | `app_navigation.test.mjs`、`workspaces.test.mjs` |
| 流式渲染、断流、中止、后台 | [streaming.py](../backend/streaming.py)、[cancellation.py](../backend/cancellation.py)、[messages.js](../frontend/messages.js)、[stream.js](../frontend/stream.js) | `test_streaming.py`、`test_background_generation.py`、`test_cancellation.py`、`stream.test.mjs`、`messages.test.mjs` |
| 模型连接、同步、历史恢复 | [powerbi.py](../backend/powerbi.py)、[reconnection.py](../backend/reconnection.py)、[chat.py](../backend/chat.py) | `test_reconnection.py`、`test_model_recovery.py`、`test_conversation_sync.py`、`connections.test.mjs`、`app_navigation.test.mjs` |
| 语义及结构变化 | [semantics.py](../backend/semantics.py)、[semantic_retrieval.py](../backend/semantic_retrieval.py)、[semantic_diff.py](../backend/semantic_diff.py)、[reports.py](../backend/reports.py) | `test_semantic_retrieval.py`、`test_semantics.py`、`test_semantic_incremental.py`、`test_report_reuse.py`、`semantics.test.mjs` |
| 字段证据、口径确认 | [fields.py](../backend/fields.py)、[values.py](../backend/values.py)、[clarifications.py](../backend/clarifications.py) | `test_fields.py`、`test_value_grounding.py`、`test_field_discovery.py`、`test_clarifications.py`、对应前端测试 |
| DAX 解析、验证与写入 | [validation.py](../backend/validation.py)、[engine/dax/](../backend/engine/dax/)、[live_writer.py](../backend/engine/context/live_writer.py)、[app.py](../backend/app.py) | `test_validation.py`、`test_write_target.py`、`test_calculated_columns.py`、`test_api.py`、`app_navigation.test.mjs` |
| 分析查询和结论 | [analysis.py](../backend/analysis.py) | `test_analysis.py`、`test_analysis_boundaries.py`、`test_generation_diagnostics.py`、`analysis.test.mjs` |
| 模型供应商与强度 | [settings.py](../backend/settings.py)、[engine/llm/](../backend/engine/llm/)、[models.js](../frontend/models.js) | `test_llm_profiles.py`、`test_reasoning.py`、`test_provider_stream.py`、`models.test.mjs` |
| 图片及多模态 | [images.py](../backend/images.py)、[frontend/images.js](../frontend/images.js)、两个 provider | `test_images.py`、`test_analysis_images.py`、`test_semantic_images.py`、`images.test.mjs` |
| 授权与发布 | [licensing.py](../backend/licensing.py)、[packaging/](../packaging/)、[发布脚本](../scripts/build_desktop.py) | `test_licensing.py`、`test_trial_licensing.py`；另行显式运行安装包验证 |

例如：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_conversation_sync.py tests\test_reconnection.py -q
node --test --test-isolation=none tests\app_navigation.test.mjs tests\workspaces.test.mjs
```

完整回归为 `.\.venv\Scripts\python.exe -m pytest -q` 和 `npm test`。依赖准备见 [README](../README.md#开发与测试)。Node 需支持现有测试参数；没有单独的前端生产构建或项目级 Python 静态类型检查命令。

### 真实环境验证

以下不属于默认离线替身测试。执行前读脚本、确认目标和权限，避免把用户数据或真实模型当作测试夹具。

| 脚本 | 验证范围与副作用边界 |
| --- | --- |
| [tests/smoke_desktop.py](../tests/smoke_desktop.py) | 显式原生窗口 smoke |
| [tests/live_smoke.py](../tests/live_smoke.py)、[tests/live_api_smoke.py](../tests/live_api_smoke.py) | 真实 Desktop 读取／查询；不是物理写入验证 |
| [verify_reconnect.py](../scripts/verify_reconnect.py)、[verify_report_reuse.py](../scripts/verify_report_reuse.py) | 报表连接、历史恢复、词典复用 |
| [verify_combined_calibration.py](../scripts/verify_combined_calibration.py)、[verify_repair_recovery.py](../scripts/verify_repair_recovery.py) | 真实引擎校准／错误和修复恢复 |
| [verify_calculated_columns.py](../scripts/verify_calculated_columns.py) | 行上下文只读抽样和 TOM 类型检查；不等于真实创建列 |
| [verify_semantic_layer.py](../scripts/verify_semantic_layer.py) | `--local-only` 本地检查；`--save-current` 会请求真实 AI 并保存词典，`--resume` 继续 |
| [verify_value_grounding.py](../scripts/verify_value_grounding.py)、[verify_field_discovery.py](../scripts/verify_field_discovery.py)、[verify_field_preview.py](../scripts/verify_field_preview.py) | 真实字段取值／归属证据；结果仍可能包含业务信息 |
| [debug_llm.py](../scripts/debug_llm.py)、[debug_analysis.py](../scripts/debug_analysis.py)、[verify_semantic_generation.py](../scripts/verify_semantic_generation.py) | 会联系配置的模型服务和／或读取真实模型，部分操作持久保存联调结果 |
| [benchmark_models.py](../scripts/benchmark_models.py)、[verify_vision.py](../scripts/verify_vision.py)、[verify_chat_transport.py](../scripts/verify_chat_transport.py) | 真实模型协议／耗时／多模态／传输检查，可能收费；先核对是否仅合成输入 |
| [verify_licensed_release.py](../scripts/verify_licensed_release.py) | 使用发布者密钥签临时测试授权，独立 AppId 临时安装、启动、到期与升级检查；不是普通单元测试 |

### 增加功能时的接线清单

1. 在 [schemas.py](../backend/schemas.py)明确输入与边界，在后端服务实现行为，不只改提示词或前端。
2. 需要持久化时区分消息原文、结构化产物、查询证据和运行状态，考虑现有数据升级。
3. 涉及 Desktop 时复用连接核对、只读限制、锁与取消；不要再建一套绕过保护的引擎调用。
4. 涉及生成时覆盖同步／流式 provider、错误事件、诊断、授权上下文与终态保存。
5. 涉及前端时绑定原 view；更新列表、当前页面、历史恢复、导出和确认交互等相关面。
6. 涉及写入时核对最终目标与当前模型，保存审计，失败不能返回成功样式，不自动重放写入。
7. 使用最相关测试证明要求；源码、运行实例、真实引擎、安装包分别说明验证范围。
8. 更新 [README](../README.md)、对应使用说明和本架构的相关章节；发布记录只记录实际完成的验证。

## 13. 尚未实现的能力

目前没有可视化看板实体、图表配置与渲染引擎、多看板组合、原生 Power BI 报表页面生成、跨模型联邦查询或公开分享服务。

未来可复用语义、查值、只读执行和状态机制，但仍需独立设计看板配置、查询调度、缓存新鲜度、版本与同步影响。不能把讨论中的方案、旧注释或辅助函数当成已经上线的功能，也不要为实现这些功能无关重写现有三种工作空间。
