# 本地开发环境

更新：2026 年 10 月 25 日。Windows 原生运行；本机已有 Windows 11 x64 实施证据，Windows 10/Server、正式服务和容量仍待验收。本页维护当前运行方法、配置与验证边界；逐轮测试、故障诊断、模型费用样本和服务重载日志已归入 [环境历史](archive/2026-10-24/DEVELOPMENT.md)，默认不读。功能状态查 [开发任务](DEVELOPMENT_TASKS.md)。

## 当前环境

| 组件 | 本机版本／位置 | 用途 |
| --- | --- | --- |
| Python | 3.13.7；项目 `runtime/venv/` | FastAPI、数据库访问、迁移及 worker |
| uv | 0.11.29；已有本机工具 | 依赖安装；`pyproject.toml` 与 `uv.lock` 锁定 Python 依赖 |
| Node.js / npm | 22.20.0 / 10.9.3 | 前端开发工具；`package.json` 与 `package-lock.json` 锁定依赖 |
| React / Vite / TypeScript | 19.3.1 / 8.3.5 / 7.0.2 | 正式网页位于 `web/`；`npm.cmd run build` 构建 |
| Tiptap / Playwright | 3.32.0 / 1.64.1 | 编辑器与浏览器测试；已安装 Chromium |
| PostgreSQL | 17.11（EDB 包 17.11-5）；`runtime/tools/postgresql/` | 唯一数据库服务 |
| pgvector | 0.8.7，使用 MSVC x64 编译并装入 PostgreSQL | 向量存储与精确检索 |
| Procrastinate | 3.10.0；任务表位于同一 `lisa` 数据库 | 持久化任务，不需要 Redis |
| NapCat | 4.18.41 Windows Node 包；`runtime/tools/napcat/` | 直接提供 OneBot 11，无需 go-cqhttp |
| Visual Studio Build Tools | 2022，17.14.42，C++ 工作负载 | 编译 pgvector；不是应用运行进程 |

API、worker 与网关沿用单一 PostgreSQL；关系数据、索引与持久任务共库。版本表是已有本机记录，安装/升级时核对依赖清单、锁文件及工具元数据。正式试用前仍须核实 D09 实际计费与重置口径。

## 正式应用运行

在项目根目录执行；凭据仍读取本地 `runtime/.env`，新配置项参见 [.env.example](../.env.example)。

日常启停入口统一放在 [launcher/](../launcher/)：双击 [Start-LISA.cmd](../launcher/Start-LISA.cmd) 启动，双击 [Stop-LISA.cmd](../launcher/Stop-LISA.cmd) 关闭。启动入口依次启动数据库、检查或应用迁移、按需构建网页、启动 NapCat 和 API/worker/QQ gateway，网页就绪后打开 `http://127.0.0.1:8000`。应用在后台运行，启动窗口关闭后继续工作；不需要另开 Vite 开发服务器。启动会按现有模型、队列、自主创作及主动联系配置正常工作。

关闭入口结束本项目的 API/worker/QQ gateway，再停止 NapCat 和数据库；保留作品、配置、数据库及 QQ 会话数据。它与启动入口互斥，不结束其他项目的 Python 或用户自行运行的 QQ。当前关闭会中断在途任务，建议先等生成完成；完整的平滑退出仍属后续服务管理验收。仅关闭浏览器不会停止后台服务。

入口通过本项目 Python 环境识别既有服务，重复点击复用已有进程；并发点击有文件锁保护。存在运行中的应用时只读核对数据库版本，需要升级则提示先停止应用，不直接迁移或重启它们。网页源码/构建配置比产物新时重新构建；首次环境搭建仍使用 `scripts/dev.ps1 Setup`。日志写入 `runtime/logs/{api,worker,gateway}-时间戳.{out,err}.log`；失败时保留窗口及已运行服务，修正问题后可重试。QQ 会话失效时仍需到 NapCat WebUI 扫码，不把进程存活等同于 QQ 登录成功。

```powershell
# 只检查启动所需文件、进程归属及端口，不启动服务或迁移
.\launcher\Start-LISA.cmd --check-only

# 后台启动，但不自动打开浏览器
.\launcher\Start-LISA.cmd --no-browser

# 只检查本项目应用进程，不停止任何服务
.\launcher\Stop-LISA.cmd -CheckOnly
```

启动编排位于 [scripts/start_lisa.py](../scripts/start_lisa.py)，停止编排位于 [scripts/stop_lisa.ps1](../scripts/stop_lisa.ps1)，复用既有依赖管理脚本，不自动安装工具、签发邀请或改变配置。两个 CMD 入口以自身位置定位项目根目录，可从其他工作目录调用。以下逐项命令继续用于开发和排查。

```powershell
# 数据库服务；保留既有数据
.\scripts\dev.ps1 Start

# 应用迁移与队列表初始化，可重复执行
runtime\venv\Scripts\python.exe -m lisa migrate

# 发放一次性邀请，明文只在此输出。初批总共最多 20 个，包含已使用名额。
runtime\venv\Scripts\python.exe -m lisa invite --count 1

# 构建网页后由同一个 API 进程提供 http://127.0.0.1:8000
npm.cmd run build
runtime\venv\Scripts\python.exe -m uvicorn lisa.app:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log

# 另一个终端运行 worker；消费聊天、作者与索引队列，配置有效时会调用模型
runtime\venv\Scripts\python.exe -m lisa.worker
# 处理当前可执行任务后退出；不是无副作用的环境探针
runtime\venv\Scripts\python.exe -m lisa.worker --once
```

需要外网模型访问的长期 worker，应在正常终端或经授权的沙箱外启动，保留本机既有代理配置。已实测受限沙箱中的后台进程会获得拒绝连接的代理地址，不能用一次前台探针成功推断后台 worker 可联网；遇到此类限制应使用授权的运行环境，不通过清空或绕过代理配置规避限制。

开发前端可用 `npm.cmd run dev`，默认 `127.0.0.1:5173`，`/api` 代理至 `127.0.0.1:8000`；`LISA_API_PROXY` 可覆盖目标。构建后的 `runtime/dist/` 由 API 启动时挂载，首次构建后再启动 API。`npm.cmd run preview` 仅预览静态产物；完整功能请使用 API 同源入口。API 不开启跨域凭据共享。

worker 同时消费 `lisa`、`lisa_author`、`lisa_index`、`lisa_style`；聊天与慢作者分队列，调用仍共用全局预算与并发。API/worker/gateway 的逐项命令为前台运行，Ctrl+C 停止；launcher 为后台编排入口。日常启动依实际配置工作，可能产生模型调用和 QQ 投递。

## 普通 QQ 私聊入口

`runtime/.env` 的 `LISA_CHAT_ENABLED=true` 开放普通私聊，`LISA_WEB_URL` 指向实际网页。已绑定且选定性格后直接发普通消息；`绑定`/`找回` 优先走不调用模型的一次性码服务。未初始化固定引导按五分钟节流，初始化后不补答旧消息；去重后按可信时间更新活跃，正常发送按账号有序并复查当前绑定。

```powershell
.\scripts\dev.ps1 StartQQ
runtime\venv\Scripts\python.exe -m lisa.gateway
```

独立测试模式：显式 `--allow-qq <本人测试QQ号>` 只接受/发送至该 QQ；`LISA_CHAT_TEST_ENABLED=true` 才接受“测试对话”前缀。正常模式不受遗留测试白名单限制；未开正常模式时使用 `LISA_QQ_TEST_USER`，无有效白名单拒绝启动。

绑定/找回码各自专用、15 分钟有效、仅一次；重新取码使同类旧码失效。找回凭据同样15分钟、一次，改密撤销旧会话。真实绑定与自定义人格保存已验；真实找回、正常模式体验及人格效果仍待验收。图片已有下述理解路径，语音等未接通媒体继续保留未读取占位。

## 开发模型配置与小样本验证

模型字段见 [settings.py](../lisa/settings.py)，占位模板见 [.env.example](../.env.example)，密钥仅存 `runtime/.env`。当前已配置默认 `gpt-6.1-sol`，聊天、作者、角色与蒸馏共用，视觉和向量采用独立百炼连接。SDK 自动重试关闭；每次新调用先预留预算和并发，HTTP 502 等未知结果保守结算且不盲目重跑。

| 配置 | 当前记录与边界 |
| --- | --- |
| 日额度 | 统一人民币数值 10 元，蒸馏共用余额、不设独立单任务金额上限；历史 `*_usd` 字段仅为兼容命名，不做汇率重算 |
| 任务额度/调用次数 | 聊天、正文和隔离试用每任务 0.15 元；聊天最多3次、含角色写作最多5次；普通风格试用1次，用户纠正及有限回归最多6次；蒸馏另有限次上限 |
| 并发/超时 | 全局并发1，开发超时120秒；未知调用保留估算费用并阻止同任务继续请求 |
| 重置 | 当前 UTC 零点；正式试用前核对供应商账单、重置时间与部署时区 |
| 主模型估算参数 | 输入2元/输出10元每百万 Token，low 推理；仅预算预留，未核实实际账单 |
| 输出/上下文 | 聊天600 tokens；正文每次最多600字符、输出1200 tokens；聊天/作者应用上限默认64000 UTF-8字节，可按已验证中转限制调整，蒸馏另配 |

作者调用直接提供共识、稳定人格、有效约束及作品资料；主 Agent 只提供稳定人格、近期聊天、记忆、生活状态和当前作品短摘要，完整作品资料由作者读取。可选召回/观察超预算可舍弃。核心内容超限时按现有固定提示或普通聊天回退，不截断约束后写作。真实合成对话、有限写作/新约束重写及图片文字记忆链路已有证据；个人作品、人格自然度、完整蒸馏和供应商故障仍待验收。历史502对照只支持间歇上游失败，未证明根治；详细请求号与费用样本只在归档追溯。

## 百炼文本向量配置与验证

本地已启用 `text-embedding-v4`；功能默认空模型关闭。独立 `LISA_EMBEDDING_BASE_URL` / `LISA_EMBEDDING_API_KEY` 必须同时填写；都留空才复用聊天连接，只填一项会停用向量。已有配置为1536维、批量10条、输入估算0.5元/百万 Token；启用前核对供应商支持及实际价格，不用虚假价格通过检查。

## 向量检索开发配置

```powershell
# 只排队；worker 消费时才产生真实向量调用和费用
runtime\venv\Scripts\python.exe -m lisa index
```

worker 每分钟扫描变更并投递 `lisa_index`。索引为派生数据，保存即排除旧修订，生成不阻塞保存；账号、作品、用途、角色和当前版本先过滤再排名。更换模型空间/地址/维度排除旧向量。原始聊天和历史正文不进入本轮向量索引；当前世界书详细条目复用既有索引，摘要/剧情状态直接提供。每批至多32片段、32000 UTF-8字节，默认召回5片段/1500字符，完整请求仍有输入上限；作者和角色可通过网页配置并查看实际引用。

所有索引/查询共用预算和调用台账；每日额度不足延期原索引任务至下个UTC日，失败或未知不自动重建同修订任务。真实中文小样本召回/跨用户过滤已验；长期语义质量和实际账单仍待核对。

## QQ 图片理解与文字记忆

本地已启用 `LISA_VISION_MODEL=qwen3.8-flash` 及独立百炼连接；默认空配置关闭。输入/输出预算估算为0.8/2.7元每百万 Token，一次最多1000输出 tokens。每批3张、每张4 MiB，仅允许校验后的QQ图片CDN，内存读取，不存原图或签名地址；结果仍属原聊天任务及其统一预算。

视觉描述是模型观察，后续聊天采用的相关记忆强制 inference、有来源且默认不用于写作；描述/原图内指令是资料。生成后和聊天终态清理临时地址，删除聊天同时清理描述；修订/取消/账号删除检查阻止迟到复活。真实视觉→Sol聊天→记忆→文本向量合成闭环已通过，实际QQ图片/CDN和长期语义仍待验收。

## 自主创作计划开发配置

`LISA_AUTONOMY_ENABLED=true` 为平台默认，显式false可关。有限延后计划复用原作者队列；开发初值为 `LISA_AUTONOMOUS_SCENE_INTERVAL_MINUTES=120`、`LISA_AUTONOMOUS_SCENE_LIMIT=2`。开始时核对当前未完结作品、共识、24小时有效活跃、预算及旧任务收尾；不补跑旧计划，手动任务不自动恢复自主。

累计次数按作品的 `last_creative_feedback_at` 统计，不随普通闲聊或作品切换清零。仅本批用户原话及实际来源时间可更新创作反馈边界，重复引文保守取最早匹配；失败/取消不返还自主尝试，在途及反馈后结束的尝试仍计入。迁移不猜测历史反馈，无记录保守计入既有尝试。计数修复已回归，真实反馈分类和多日效果继续验收。

## 主动联系开发配置

`LISA_PROACTIVE_ENABLED=true` 为平台默认，账号联系偏好独立；新账号初始关闭，本地本人账号已允许。账号页显式保存开关、IANA时区、免打扰、间隔和次数；GET不新建偏好或刷新活跃。开发初值240分钟最短间隔、滚动24小时最多2次，最新反馈后最多一次尝试，失败/取消不返还机会。

生成、采用和发送前均复查24小时活跃、用户设置、免打扰、频率、版本及预算。资格过期不补发，不适用有限创作收尾例外；送达后才写交流及分享状态。交流观察、未决话题暂定方案与适时分享已保存成果已接通，不能将默认设计当共识。普通回复及用户明确QQ委托的结果通知沿既有规则。真实主动话题、QQ投递与多日自然度仍待验收。

## 异常退出与任务恢复

`lisa/recovery.py` 在worker启动和分钟扫描中复用Procrastinate心跳及数据库执行锁，失联至少60秒才处理，仍持锁的慢任务不接管。无模型调用的中断任务沿原标识/范围有限续跑并重查资格，队列累计尝试最多3次。遗留reserved保守转unknown并计原预算日，释放并发，不退款/重放；已有调用但未采用不重播整条流程。

已采用不重复、取消不复活，终态清理临时语料/图片URL；扫描按当前资格运行，不补旧计划。网关重连将sending转unknown，不盲重发，pending按原顺序续送。`tests/test_recovery.py` / `recovery_child.py` 已完成真实隔离子进程强杀/重启验证，模型与OneBot为合成替身；真实供应商、账单及NapCat登录会话恢复仍待验收。

## 作品与章节网页分享

`LISA_WEB_URL` 为公开链接基址，只允许无认证/查询/片段的绝对HTTP(S)地址。默认本机 `http://127.0.0.1:8000` 不具外部可达性；部署时把 `/read/` 与 `/api/` 转发到API。分享随机令牌只读已保存正文，用户可复制/收回，账号停用/删除即失效；Agent可按系统校验后的本人章节自主分享，不加逐次审批。网页与隔离工程已验，真实QQ点击/模型分享和外部可达性仍待验收。

## B09 风格蒸馏实现与验证

常驻“性格与风格”导航，性格页可直达QQ蒸馏，创作资料可直达所选作品写作风格；书架与完整任务分页，未保存保护仍生效。QQ TXT提炼交流性格，作品TXT/Markdown提炼写作风格；多个方案/固定版本、隔离试用、明确应用及默认/作品覆盖均已接通。

临时语料存独立表，成功/最终失败/取消/放弃清空原文和解析副本，未启动会话默认24小时到期；终态与清理同事务，结果只保留概括及来源定位，不留例句库。恢复与到期清理独立；账号删除覆盖新增数据。开发边界：16文件、8 MiB上传、单批24000字节、请求64000字节、最多28次调用、每次1600输出 tokens；超限明确缩小/拆分，不静默截断。真实QQ合成短样本成功，完整作品蒸馏与个人长篇效果仍待验收。

## QQChatExporter V5 格式兼容验证

上传及独立CLI共用解析器，支持V5昵称/时间/内容分行TXT及旧TXT/UTF-16/GB18030；已知空字符以 `�` 兼容处理，按用户后续要求不显示新旧上传的空字符技术提示，消息数不符等分析警告保留，页头/统计不计消息。真实导出样例只读解析14,243条消息、90个昵称标签通过，文件哈希不变；标签数不等于已核实身份数。格式与页面回归通过，其他变体、同名身份和真实人格效果继续验收。

选择人物后的模型失败会显示台账中安全的 HTTP 状态码，unknown 仍停止、保守结算并清理原文，不自动重试。最近两次真实首批为中转 HTTP 502；合成同等规模请求既有成功也有502复现，完整短QQ合成蒸馏已成功，目录可访问不等于生成稳定。未登录通道监控返回401，具体上游原因仍须站点查日志；不把合成成功当作个人完整效果验收。诊断与回归见 [环境历史](archive/2026-10-24/DEVELOPMENT.md#qq-蒸馏-http-502-与技术提示调整)，当前状态复核见 [中转检查](archive/2026-10-24/DEVELOPMENT.md#中转站当前状态复核)。

## 聊天蒸馏的目标窗口与本地工具

2026 年 10 月 24 日将聊天分析改为 `lisa-style-v2-target-windows`。`lisa/chat_corpus.py` 是纯标准库本地索引与窗口工具，网页上传解析复用同一解析器；修正参考导出器每行 seq/id/type 变化误作不同身份的问题，保留消息编号、文件内身份与可识别会话、时间及原始分条。后端先索引元数据，按目标发言提取邻近窗口，无关群消息不进入模型；窗口原子打包，超预算单条跳过并计数，预算内按会话与时间分层取样。上下文角色与目标角色分别标注，跨窗口不当作连续来话；报告区分范围内目标数、分析目标数、邻近上下文、取样窗口与未覆盖数量。作品分析仍覆盖明确选区，不采用聊天取样替代长篇分析。

配置新增 `LISA_STYLE_CHAT_CONTEXT_BEFORE=4`、`LISA_STYLE_CHAT_CONTEXT_AFTER=2`、`LISA_STYLE_CHAT_MAX_WINDOWS=24`；实际窗口数继续受到任务调用上限收紧。窗口默认不超过 24 条、16,000 序列化 UTF-8 字节、距锚点 30 分钟，且不得超过单批输入预算；这是检索初值，不是话题完整性证明。原始上传及解析副本继续在终态清理，取样不会扩大保留范围或改变统一每日预算。

本地工具可先索引，再按索引中的目标 ID 提取，均输出到新的私人目录，不覆盖输入：

```powershell
runtime/venv/Scripts/python.exe -m lisa.chat_corpus "D:/private/chat.txt" --out "D:/private/style-index"
runtime/venv/Scripts/python.exe -m lisa.chat_corpus "D:/private/chat.txt" --target "0:p1" --out "D:/private/style-windows"
```

`index.json` 只有来源/身份计数及覆盖信息，不含消息正文；`windows.jsonl` 每行一个有限窗口，仍是私人原文副本，不放 Git、skill 或长期报告。stdout 仅计数，`validation=not_executed`。用户随后要求删除独立 `chat-style-distiller`，现已删除且不保留同步副本；LISA 内置方法与网页解析继续维护。

BAK上传转换及NTQQ本地库直读已由用户取消，不继续取钥、接口适配或替换原链路。独立CLI产生的私人原文副本不放Git或长期报告。

当前聊天方法为 `lisa-chat-v3.1-natural-expression`，`lisa/chat_style.py` 处理依赖组、盲测输入、目标消息统计与概括证据；`chat_distillation.txt` 定义条件化提炼及去模板审查，`chat_expression.txt` 在普通聊天、主动联系和隔离试聊共用。作品方法版本保持原值。原文例句只临时用于分析/评测，最终报告不保留生成回复、草稿或测试答案；指标只作审查线索，不是 AI 检测器。

聊天计划预先包含基线/完整草稿/压缩候选开发对照、一次修订及冻结检查。自动合并共享消息、引用、重复内容和相邻窗口后，可从同一长文件建立有边界的开发/留出；跨时段依赖仍标未人工核实。无真实留出时仅执行明确标记的合成开发/回归，不假称独立验证。所有新增调用计入原有最多28次与每日统一预算，不自动重试未知结果。

`POST /api/style/trials` 的 `mode=refine` 接收当前指引、`input` 情境、`wrong` 回复与 `correction` 期望修改，最多6次调用生成概括候选及原/新指引回归。沿用试用金额上限、取消/恢复/一天到期清理；终态抹除纠正原文，保存版本只信任本账号完成任务的报告。网页先载入候选，再保存与明确应用；不会自动改已有性格。纯合成真实调用可用 `runtime/venv/Scripts/python.exe -m scripts.probe_style --live --kind personality`，仍需如实区分上游失败、管线成功和人工自然度。

方案操作补充：`POST /api/style/tasks/{id}/discard` 只接受已完成且含候选的蒸馏/修订任务，重复调用安全；丢弃状态存于既有任务 JSON，列表/详情隐藏结果，保存时拒绝再引用该候选，不新增迁移。`DELETE /api/style/profiles/{id}` 删除本账号方案及全部版本，任一版本仍被 LISA 或作品引用时返回 409；成功后来源任务不再作为历史候选展示。页面仅恢复正在执行的任务，已完成历史结果须手动载入。交互与隔离验证见 [B09 修复记录](archive/2026-10-24/DEVELOPMENT_TASKS.md#b09-候选丢弃与方案删除)。

## 群记录采集与管理员设置

D17网页群消息来源、检索蒸馏及管理入口已移除；后端、既有授权与30天滚动档案保留，网页继续提供QQ TXT/作品上传。仅已配置启用群可被普通网关被动采集，群内不回复，不自动回填历史；测试白名单模式不采集。原授权、来源版本/期限和迟到结果检查继续约束后端路径，不因D20全体共同维护公共资料而取消私人群权限。维护既有管理员时使用 `python -m lisa admin --username EXISTING_USERNAME --qq-id BOUND_QQ_ID`，不新建或替换绑定；历史群页面验收不作为当前页面入口。

## B10 共有表情图库验证

迁移 `a925d018ca10`、`b741e065ab20`、`c02b7f3a914` 新增并收缩共有条目、版本、素材和个人偏好/屏蔽，并为既有 Task、ModelCall、IndexState 增加受类型约束的平台归属；API/worker/数据库和文本向量设施继续复用。所有有效登录用户使用相同 `/api/shared` 维护入口，无管理员或审批。图片以最多4 MiB的PNG/JPEG/GIF/WebP存于数据库，指纹复用；上传须明确贡献，不从私聊或群记录自动入库。

B 站热点雷达已按用户反馈移除：不再访问作者 JSON、不再执行定时拉取或聊天触发补查，也不再保留热点共有条目和公共索引。共有记忆相关模型调用仅服务表情条目分析与公共索引；`LISA_SHARED_DAILY_USD=1.00` 继续作为共有表情维护额度，计入既有10元总额。


`/api/shared` 提供列表、新增和带版本保存，`/{id}/versions`、`/{id}/restore` 提供版本及恢复；`/assets` 接收带贡献声明的有限二进制图片，`/preferences` 与 `/{id}/mute` 只影响个人，`/{id}/analyze` 复用平台有限模型任务。`/jobs/{id}` 提供表情分析任务状态；雷达状态和用户刷新接口均已移除，不暴露私人来源或模型凭据。普通聊天/主动联系只选择本轮提供的有效素材ID，网关在实际出站前复查版本、撤下和偏好，组装文字与图片段；失败/未知沿用原投递记录。已送达表情可按当前有效素材补充聊天上下文，不将未发送图片当作已经告知。

隔离验证：迁移升降升、Alembic差异、队列初始化及worker单次执行通过；完整后端755项通过，后续相关109项定向通过，最终B10共17项再通过。原46项Chromium已通过，新增真实API双用户用例修正对既有找回账号的夹具依赖后通过，并在最后样式调整后再次通过；桌面与390px截图已独立复核。覆盖平等维护、旧版409及保留输入、指纹与贡献、撤下/恢复、个人屏蔽、角色/私人隔离、模型中途撤下、来源失败、双重额度、平台异常恢复与账号清理。构建/TypeScript、Ruff和文档检查按本轮结果核实。可用 `scripts/test_app.py --pytest-target tests/test_shared.py --browser --browser-target shared.spec.ts` 在一次临时库中定向复查，两个target参数均可重复；未指定时保留完整检查。

本地加载：确认活动任务、reserved调用和待发均为0后，仅停止本项目API/worker/gateway，升级并重载；PostgreSQL、NapCat、QQ会话和个人资料保留。新版健康、OpenAPI及数据库版本确认通过。首轮真实快照时间为 `2026-10-24T04:15:45+00:00`，入库20条视频及15个待补充词语；20条真实公共向量索引和一条纯公共查询的3条召回引用通过。摘要调用当次上游HTTP502，台账记unknown并保守计预估消耗、不自动重试，条目保留来源简介及其事实边界；不将其记为真实摘要成功。没有人为发送QQ测试消息或向线上写合成账号/表情。真实QQ图片投递、个人素材/人格自然度、多日相关性和供应商账单仍待验。

## B11 对话节奏与分条投递验证

迁移 `b11a6d2e91f7` 为 `outbound_messages.interval_seconds` 增加持久化发送间隔。聊天模型通过 `Conversation.message_intervals` 为同轮 `messages` 选择条间等待，首条固定立即发送；应用将每项限制为0.5—8秒，每轮最多6条，累计等待不超过30秒，缺失或无效值回退1秒。后续条目只在上一条获得明确 `sent` 后，以实际完成时间重新计算 `next_attempt_at`，不会因队列积压或网络变慢一次性补发。

有效新 QQ 消息仍立即取消尚未发送的旧轮次尾项；正在发送的请求等待网关真实结果，未知不记为已告知。投递选择按用户顺序保留旧在途轮次的屏障，旧尾项不自动复活，新消息继续走10秒静默合批。`tests/test_chat.py` 覆盖间隔边界、实际成功基准、在途打断和新轮次屏障；本轮隔离回归已通过。真实 QQ/NapCat 多条发送、进程重启恢复和多日自然度仍待实机验收。

## 世界书条目与当前剧情验证

D19迁移 `d8193f6a20b4` 保留原worldbook为固定核心，JSONB详细条目按需召回，一层直接关联共用预算与引用。摘要/剧情状态直接提供，仅检索来源变化使世界书索引失效。条目管理、保存/失败/离开保护与隔离回归已验，真实长篇召回、年代/例外遵循仍待评；不增加图谱、时间模拟或自动事实修复。

## 账号整体删除与默认作品页

当前密码确认后同事务停用并清理关联个人数据，成功才退出；在途结果和重试不得恢复内容。已发送QQ不可撤回，匿名已消耗预算及邀请占用保留。初始化完成默认进入“我的作品”。工程和隔离页面已验，不用真实账号删除作为普通测试。数据库/NapCat个人原文日志已关闭持久化，应用保留匿名诊断；不可用删除runtime代替账号清理。

旧环境的 `scripts/dev_env.py privacy-cleanup` 会不可恢复清除历史诊断，须明确授权且相关服务停止；不是日常启动或账号删除步骤。数据范围以主方案和Git协作为准。

## 小说插图模型预研

B15 当前工程入口：[`lisa/images.py`](../lisa/images.py) 复用现有 `Task`、队列及 OneBot 出站，迁移为 `b15e8a102810`；`PrivateImage` 二进制随账号级联清理，和共有表情 `SharedAsset` 分开。独立 `LISA_IMAGE_MODEL/BASE_URL/API_KEY` 沿用本地配置；`LISA_IMAGE_TIMEOUT_SECONDS=180`，medium/1024×1024/n=1/PNG，无 SDK 自动重试。没有参考用 `/images/generations`，明确参考用 multipart `/images/edits`。上传支持 PNG/JPEG/WebP，最多 12 MB、8192px 单边与 3200 万像素，解码后去除元数据并存 PNG；这些是文件有效性限制，不是图片消费额度。

纯图片回复：`ImageAction.send_text_reply=false` 可省略本轮额外文字确认，`caption` 独立决定图片配文；send_text_reply=false 且 caption 为空时只交付裸图，也允许不先确认、只发图片加短配文，默认不附完成套话。配文只在图片任务临时快照及待投递消息中保存，终态清理；不写入永久回复历史。OneBot 出站省略空文本段，图片缺失时取消空投递；纯图轮的未发送文字不提升为已分享生活事实。2026-10-28 已通过 219 项相关隔离回归及两条真实合成描述验证，空闲重载 worker/网关后已加载；网关 normal 连接和 API 健康已核对，现有 corpus 模式不变，无需迁移。提交前独立纯图快照完整后端 855 项通过，补齐参考失效的文字反馈；该最终修复未再次重载。本轮未实际生图/QQ 实发，交互实效继续验收。

明确绘图话题用专用 `ImagePreparation` 准备有限绘图描述，不经过普通聊天/检索额度；正常聊天可选择一张契合语境的小图。生图和描述台账均标记 `budget_exempt`，不增加文本日账；生图有独立执行槽。LISA 出镜必须先由该账号明确指定参考，测试参考不自动设给正式账号。新消息不取消独立成图；QQ 失败只重发已有图片，unknown 不自动重画；聊天图片约 24 小时后由现有 recovery sweep 清理，已替换且无需投递的作品旧图不作历史保留。

接口：`GET/PUT /api/projects/{id}/cover`、`GET/PUT /api/chapters/{id}/illustration`，上传 JSON 含 `content_base64`；`GET /api/private-images/{id}` 要求本人登录，公开阅读页不引用它。取消复用 `/api/tasks/{id}/cancel` 或对话明确取消，作品任务列表显示图片状态，不提供网页生图面板。

2026-10-28 新业务在临时库中实际完成合成封面/章节及指定参考人设图，耗时 99.147 / 34.012 / 99.837 秒（含描述与生成），保存/出站段正确；文字日账预置 999 元仍为 999 元。usage 全零仅记价格未知。脱敏参数/台账及输出在 `runtime/reports/b15-images-20261028/{result.json,recheck.json,cover.png,illustration.png,chat.png}`；章节首次被解释成仅保存画风，修正操作优先级后复验成功。图片版网页构建保留于该目录的 `dist/`，当前 `runtime/dist/` 恢复为接入图片前的作品页，避免未重载 API 显示不可用入口；源码保留完整新界面。

首轮业务交付时没有迁移个人库、重启正式进程或发送真实 QQ；本轮纯图加载记录见上方。新环境接入时在合适空闲窗口运行 `runtime/venv/Scripts/python.exe -m lisa migrate`，使用包含 `lisa_image` consumer 的 worker/网关并重新构建网页；不能只复制新网页。全部源码仍含其他任务未提交改动，加载前需核对这些任务的生效范围。测试/限制详见 [B15](DEVELOPMENT_TASKS.md#b15-小说插图预研)。以下早期预研记录仅表示当时结果。

当前模型为 `gpt-image-2`，独立密钥/端点保存在 `runtime/.env` 的 `LISA_IMAGE_*`。按用户本轮提供参考图的明确授权，已向 `https://drc.totokens.cc/v1/images/edits` 实际上传 PNG，medium、1024×1024、n=1、PNG 输出，HTTP 200，24.895 秒，返回模型 `gpt-image-2`，得到一张挥手半身插画。已目视检查，主要发型、红眼、头饰及服装主配色与参考图相近，构图和动作变化；符合用户仅锚定基础风格的目标，不做严格身份一致性判定。usage 所有字段为 0，不能据此判定免费。

输出为本机忽略目录 `runtime/reports/image-reference-probe-20261028/reference-result.png`；`reference.png` 为用户指定测试输入，`result.json` 保存脱敏参数、完整提示词、请求号、时间及文件哈希。本轮只发一次 multipart 请求，自动重试 0，密钥仅由本地配置进入请求、不输出；无业务代码、配置、数据库、QQ 或进程重载动作。完整证据见 [参考图实测](archive/2026-10-24/DEVELOPMENT.md#2026-10-28-参考图输入实测通过)。

用户已取消 D25 生图及提示准备的全部应用内消费额度，不再要求日额/任务金额/调用次数封顶、费用预留或价格配置作为启用门槛；统计与额度拦截分离，其他文本功能限额保持原状。当前正式图片业务尚未实现，需在后续接入中落实此豁免，不能把配置或独立探针当成业务已启用。此前配置切换证据见 [历史记录](archive/2026-10-24/DEVELOPMENT.md#2026-10-28-totokens文本与生图配置切换)。

以下为旧站、旧模型的历史预研，不代表新配置的可用性：

2026 年 10 月 28 日，用户指定 `gpt-image-2.5-sunburst` 并明确授权本轮密钥发送至项目现有 `https://test.mcii.cc/v1`，查询模型及最多生成一张低质量合成测试图。独立探针的 `GET /models` 返回 HTTP 200、0.791 秒，精确模型名存在；唯一一次 `POST /images/generations` 使用 `n=1`、`size=1024x1024`、`quality=low`、`output_format=png`，0.789 秒返回 HTTP 503、`No available compatible accounts`，没有图片或 usage，未重试。

结论是“鉴权与清单可见，实际生图尚不可用”，不能据此确认尺寸/质量参数兼容、后端身份、图像质量、计费或长期稳定性。先请中转供应商核对该密钥/分组的模型路由与兼容账号资源；没有自动切换其他模型或协议。此次独立供应商探针未接 LISA 任务账本，实际费用未知，生产接入必须另按统一预算及真实计价实施。

密钥只经探针进程标准输入使用，没有写入文件、正式环境配置或文档；仅合成场景出站，不含私人小说/聊天。没有启用功能或重载服务。详细脱敏记录及本地证据路径见 [探针记录](archive/2026-10-24/DEVELOPMENT.md#2026-10-28-小说插图模型连通性预研)，需求见 [D25 草案](PROJECT_PLAN.md#小说插图需求草案)。

## 正式应用检查

### 隔离回归与构建

维护检查补充：若旧 `runtime/tmp` 受本机 ACL 限制，可在当前测试进程设置 `PYTEST_ADDOPTS=--basetemp=runtime/reports/<本轮唯一测试目录>`，避免复用其他测试的临时目录。数据库未启动时，只需运行 `runtime\venv\Scripts\python.exe scripts/dev_env.py db-start`；不必启动完整 launcher、QQ 网关或真实 worker。Windows 受限沙箱的 `pg_ctl` restricted-token 错误、Vite `spawn EPERM` 与业务回归失败分别记录；应在获准的正常执行环境运行，不修改凭据或关闭隔离。

诊断日志：API 记录请求编号、注册路由模板、状态码及 `duration_ms`，不记录实际 URL 参数、查询字符串和正文；worker 记录任务编号/类型、`task_age_ms`（自创建以来的累计时间，包含延迟及重试）和本次 `execution_ms`。`outcome=returned` 仅表示执行函数返回，不替代数据库中的任务成功/失败状态。网页读取默认 30 秒超时、写操作默认 120 秒，周表同步生成单独允许 210 秒；超时不自动重复写操作，登录失效保留输入并等待原账号确认。

```powershell
# 真实 PostgreSQL，自动创建独立 lisa_test_<UUID> 数据库并在结束后删除
runtime\venv\Scripts\python.exe scripts/test_app.py
# 再执行真实网页流程；测试 API/Vite 分别使用 8011/5174 端口
runtime\venv\Scripts\python.exe scripts/test_app.py --browser

npm.cmd run build
runtime\venv\Scripts\python.exe -m ruff check lisa migrations tests scripts
runtime\venv\Scripts\python.exe -m alembic check
git diff --check
```

测试统一放在 `tests/`：Python 的 `test_*.py` 与 `conftest.py` 保留在该目录，Playwright 的 `*.spec.ts` 位于 `tests/e2e/`，由根 `playwright.config.ts` 指定。两套测试仍通过 `scripts/test_app.py --browser` 在同一次隔离运行中先后执行。

测试使用本地管理员凭据创建临时库、普通应用角色执行测试，不往个人应用库写合成账号；临时库名称验证、迁移升降升、`lisa migrate` 重复执行、worker 启动及持久任务均包含在检查中。浏览器检查需已有 Chromium，不连接真实 QQ 或模型。截图及 Playwright 产物保存在忽略的 `runtime/reports/playwright/`。不要直接把测试环境指向个人数据库。

## 日常使用

在项目根目录的 PowerShell 中执行：

```powershell
# 启动数据库；首次创建数据库、pgvector 与 Procrastinate 表
.\scripts\dev.ps1 Start

# 数据库、向量、中文数据与实际 worker 执行检查
.\scripts\dev.ps1 Check

# 启动 NapCat（独立开关，默认后台运行）
.\scripts\dev.ps1 StartQQ

# 登录后只读检查 OneBot 版本和登录状态，不发送消息
.\scripts\dev.ps1 CheckQQ

# 前端类型检查与构建；浏览器流程使用上方隔离回归入口
npm.cmd run build

# 停止服务，保留数据库与会话数据
.\scripts\dev.ps1 StopQQ
.\scripts\dev.ps1 Stop
```

启动数据库通常不需要管理员权限。受限沙箱可能阻止 Windows 令牌创建、本机 TCP 连接或浏览器启动，需要在普通终端或经授权的沙箱外执行；不要为此关闭数据库认证。当前没有注册开机自启动服务，电脑重启后可双击 `launcher/Start-LISA.cmd` 启动整套应用；上述 `Start`、`StartQQ` 保留为独立依赖命令。

| 连接 | 地址／配置 |
| --- | --- |
| PostgreSQL | `127.0.0.1:55432`，数据库 `lisa`，普通角色 `lisa` |
| OneBot 正向 WebSocket | `ws://127.0.0.1:3001`；请求头 `Authorization: Bearer <LISA_ONEBOT_TOKEN>` |
| NapCat WebUI | `http://127.0.0.1:6099/webui`；令牌为 `runtime/.env` 的 `LISA_NAPCAT_WEBUI_TOKEN` |

密码与两个不同的令牌由 `init` 随机生成，只保存在本地 `runtime/.env` 与 NapCat 运行配置中。数据库超级用户为 `postgres`，应用使用非超级用户 `lisa`。本地服务仅监听回环地址，不需要添加公网防火墙放行规则。

`Start` 会保留已有数据，`init` 不覆盖现有 `runtime/.env` 和 NapCat 配置。修改数据库密码不能只改 `runtime/.env`，还须同步数据库角色；修改 OneBot／WebUI 令牌须同步 `runtime/napcat/config/` 中的实际配置，必要时也检查 NapCat 创建的账号专属配置。数据库端口由 `runtime/.env` 控制；QQ 端口当前固定为上述开发端口，若调整须同步配置与客户端。

## NapCat 登录与数据

首次运行 `StartQQ` 后，在 WebUI 扫码登录；也可打开 `runtime/napcat/cache/qrcode.png`。二维码会过期，以运行中的 WebUI 为准。已有账号登录后，可在本地 `runtime/.env` 填写 `LISA_QQ_ACCOUNT`，脚本下次启动用 `-q` 选择该账号进行快速登录；会话失效时仍需重新扫码，不能保证永久免登录。

采用 NapCat 官方 Windows Node 独立包及其内置 QQ 运行组件，不调用附带的 `KillQQ.bat`，不结束用户已有的 QQ 进程。NapCat 的配置、缓存与日志通过 `NAPCAT_WORKDIR` 放到 `runtime/napcat/`；QQ 底层账号会话仍遵循 NTQQ 的本机用户数据目录，可能位于仓库外，不能把 `runtime/napcat/` 当成完整 QQ 会话备份。部署时使用专用 Windows 运行账户和机器人 QQ 登录上下文，再验证断开远程桌面、重启与会话恢复。

`CheckQQ` 只查询版本与身份，不发送消息，也不会把收到的聊天存入数据库。服务指令、绑定及受限对话测试使用上面的应用入口；普通私聊现可由正常开关开放，真实 QQ 模型链路已有早期测试证据和用户确认；本轮无前缀多账号的真实使用及人格效果仍待用户验收。

## 在新机器上重建

先安装 Python 3.13 x64、Node.js 22（至少 22.20.0）、uv，以及带 C++ x64 编译工具和 Windows SDK 的 Visual Studio Build Tools。安装器以官方发布渠道为准；本机 C++ 工作负载由以下命令安装：

```powershell
winget install --id Microsoft.VisualStudio.2022.BuildTools --exact --silent --accept-source-agreements --accept-package-agreements --override '--quiet --wait --norestart --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended'
```

Windows Server 未必已有 winget，可通过微软官方 Build Tools 安装器添加相同工作负载。安装完成后重新打开终端，再运行：

```powershell
.\scripts\dev.ps1 Setup
.\scripts\dev.ps1 Start
.\scripts\dev.ps1 Check
npm.cmd run build
.\scripts\dev.ps1 StartQQ
```

`Setup` 通过 `scripts/uv.ps1 sync --locked` 同步到 `runtime/venv/`，并使用 `npm ci --cache runtime/cache/npm`，从官方发布地址下载固定版本的 PostgreSQL、NapCat 和 pgvector 源码，校验 SHA-256，使用 MSVC 编译扩展，生成本地配置并安装 Playwright Chromium。下载约 500 MB，另加依赖、浏览器和 C++ 构建工具；解压与工具安装需要更多磁盘空间。脚本不会自动重启电脑。

下载源与摘要维护在 [下载脚本](../scripts/download_native.py)。NapCat 摘要来自 GitHub release 元数据；PostgreSQL 与 pgvector 摘要记录本轮通过官方 HTTPS 地址下载的固定归档。工具的版本、来源和归档摘要也写入各自 `lisa-tool.json`。已存在不同版本时脚本报错，不自动覆盖升级。数据库大版本升级需要迁移，不能只替换二进制目录。

后续安装新 Python 包使用 `.\scripts\uv.ps1 add`／`.\scripts\uv.ps1 add --dev`，同步提交清单与锁文件；前端使用 `npm install --save-exact`，同步提交 `package-lock.json`。不通过全局安装应用依赖来绕过项目锁定。

## 文件与验证边界

本地环境与生成物统一集中在 `runtime/`，根目录保留源码、工具必需的配置与清单，以及 npm 标准位置的 `node_modules/`：

```text
runtime/
  .env          # 本机凭据与运行配置
  venv/         # Python 虚拟环境
  cache/        # uv、npm、pytest、Ruff、Vite 等可重建缓存
  tmp/          # pytest 专用临时文件，每轮测试重建
  dist/         # 网页构建，API 从这里挂载
  reports/      # Playwright、模型探针与历史诊断结果
  secrets/      # 本地邀请等私人文件
  tools/        # 原生工具
  postgres/     # 实际数据库，不能当缓存删除
  napcat/       # 网关配置与会话相关文件
  logs/         # 进程日志
```

同步 Python 环境使用 `.\scripts\uv.ps1 sync --locked`；增加依赖也经此入口，避免直接运行 uv 时重新生成根目录 `.venv/`。`scripts/dev.ps1 Setup` 已使用相同入口。升级旧目录结构时，`init` 会把唯一的根 `.env` 移到 `runtime/.env`；两处都有文件时拒绝覆盖，须先核对配置。Python 虚拟环境按锁文件重建，不直接移动含绝对启动路径的旧环境。

需要集中 Python 字节码时，在当前终端执行 `$env:PYTHONPYCACHEPREFIX = "$PWD/runtime/cache/pycache"`。仅清理时可删除已停止使用的缓存和临时测试目录；报告按需清理，数据库、QQ 配置与日志保留规则见下表及 Git 协作。历史验证描述中的命令与产物路径已统一为当前结构。

| 内容 | 位置／处理 |
| --- | --- |
| Python 依赖与缓存 | `runtime/venv/`、`runtime/cache/uv/`，忽略 |
| 工具缓存、测试临时文件、报告 | `runtime/cache/`、`runtime/tmp/`、`runtime/reports/`，忽略 |
| 前端依赖 | `node_modules/`，忽略 |
| Chromium | 本机用户目录下的 `ms-playwright` 缓存，不在仓库 |
| 原生工具、下载与编译文件 | `runtime/tools/`、`runtime/downloads/`，忽略 |
| 数据库数据 | `runtime/postgres/data/`，忽略 |
| NapCat 配置与缓存 | `runtime/napcat/`，忽略；账号专属文件也不得提交 |
| 进程输出 | `runtime/logs/`，可能包含 QQ 内容、二维码和 WebUI 登录链接，忽略 |
| 密钥、账号 | `runtime/.env`，忽略；仅 `.env.example` 可提交 |

不要删除 `runtime/` 来清理构建产物，它还包含实际数据库和网关配置。按 D10 于 2026 年 10 月 23 日确认的范围，首版不建设备份机制，不安排备份轮换或恢复流程；普通正文版本用于改稿恢复，仍保存在业务数据库内。用户导出仅限作品正文，不提供个人资料包。账号整体删除及其他数据清理按项目方案实施。

Windows异步worker沿用SelectorEventLoop；正常探针不代替业务故障验收。M09-07已完成隔离实进程恢复，正式服务平滑退出/自启动、真实供应商和NapCat会话恢复、Windows 10/Server、多日稳定性与容量仍未验收。WSL/Docker/Hyper-V不是当前项目依赖；系统组件操作历史仅在归档追溯。

## 历史验证索引

完整逐轮证据见 [环境归档](archive/2026-10-24/DEVELOPMENT.md)，任务规格和实施表见 [任务归档](archive/2026-10-24/DEVELOPMENT_TASKS.md)。定位目标标题或任务ID后分段读取；旧“当前开关/模型/尚未实现”不覆盖本页和当前代码。本轮聊天方法的隔离验证与真实模型限制见 [B09 自然度优化](archive/2026-10-24/DEVELOPMENT_TASKS.md#b09-聊天自然度优化)，未发送 QQ 或重启常驻服务。
