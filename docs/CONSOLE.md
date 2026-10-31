# 控制台使用说明

本地网页控制台是机器人的"驾驶舱"。Bot 启动后它跟着一起起来，关掉 Bot 就没了。

> 这份文档只讲控制台。安装、`.env` 配置、NapCat 接入、QQ 里的命令，见 [README](../README.md)，这里不重复。

---

## 打开与登录

```text
http://127.0.0.1:8080/admin
```

1. 第一次打开要输入 `.env` 里的 `ADMIN_TOKEN`。
   - 用「一键启动.bat」配的：密码就是配置向导最后打印的那一串，已经自动生成好了。
   - 忘了：打开 `.env` 搜 `ADMIN_TOKEN`，或者双击「配置向导.bat」重设一个。
2. 输入一次后浏览器会记住（存在 localStorage），下次直接进。
3. 想换 token：改 `.env` 里的 `ADMIN_TOKEN`，重启 Bot，然后在登录页点"退出"重新输入。

如果页面提示 **"控制台未启用：请在 .env 设置 ADMIN_TOKEN"**，说明 `ADMIN_TOKEN` 是空的——控制台被主动关掉了，填一个值再重启即可。

端口不是 8080 的话，看 `.env` 的 `PORT`。

---

## 顶栏：先看这 6 个数字

打开就能看见，不用点任何面板：

| 指标 | 含义 |
|---|---|
| OneBot 连接 | WebSocket 是否连上 NapCat。断开时这里会是红的 |
| 运行时长 | 本次进程活了多久 |
| 当前人设 / 模型 | 正在用的是谁、调的哪家服务商的哪个模型 |
| 消息数 | 累计处理的消息条数 |
| 用户数 / 群聊数 | 有多少人、多少个群在用它 |
| 记忆数 / 大脑笔记数 | 长期记忆和资料库的体量 |

连不上 NapCat 时，先看这里，再去下面的"错误"面板。

---

## Token 面板

控制台最显眼的那块。分三层：

- **今日**：总消耗、输入 tokens、输出 tokens、调用次数；
- **预算进度条**：`DAILY_TOKEN_BUDGET` 的完成度，超 `TOKEN_WARN_RATIO` 会变色并在 QQ 里提醒一次；
- **近 7 天柱状图 + 按来源统计**：能看出是聊天在烧，还是表情打标签/风格学习/自我批评在烧。

调用次数突然飙升，通常是这两个原因：

1. `REPLY_MAX_BUBBLES` 或上下文太长（`CONTEXT_MESSAGES`）；
2. 群被切到了"全量模式"，每条消息都回。

改 `.env` 里的 `MAX_OUTPUT_TOKENS`、`CONTEXT_MESSAGES` 最有效；急刹车就把 `DAILY_TOKEN_BUDGET` 设成一个具体数字。

---

## 技能权限矩阵

一行一个技能，一列一个维度：

- **全局开关**：关掉后这个技能在哪儿都不能用；
- **最低角色**：`member` / `admin` / `group_owner` / `owner`，角色从高到低是 `owner` > `group_owner` > `admin` > `member`；
- **按范围覆盖**：给某个群或某个私聊单独设，粒度到"这个群的踢人只给群主"；
- **机器人群身份**：显示机器人当前在每个群里是成员 / 管理员 / 群主。不是管理员时，群管理类技能即使权限够了也会失败——这是 QQ 的限制，不是 bug；
- **最近审计**：谁在哪个群请求了什么、执行了还是拒绝了、失败原因。

两个常被误解的点：

1. **QQ 里改不了权限。** 只有控制台能改，避免有人在聊天里把权限聊上去。
2. **权限放开 ≠ 一定执行。** 还要过"自主判断"和账号硬边界（不加好友、不支付、不主动私聊等，代码级禁止，不随权限变化）。

---

## 自主权限（控制台 > 大肥鱼 > 其他指令）

- **允许大肥鱼自己判断**：开 / 关。
- **指令框**：600 字以内，写你想让它遵守的做事风格，比如"群里让我踢人时，先看对方是不是在刷广告"。

行为差异：

| 状态 | 表现 |
|---|---|
| 开（默认） | 每条有副作用的命令先让模型判断 allow/deny，它可以拒绝，拒绝理由会回给请求者并写审计 |
| 关 | 不做判断，直接按技能权限执行 |
| 探索模式 | 所有技能请求直接放行，只保留法律和五条硬规则 |

只读命令（`/大脑 查`、`/大脑 看`）不触发判断，不烧 token。判断结果缓存 `AUTONOMY_CACHE_SECONDS` 秒。

> 指令框写的东西**不能覆盖**硬边界。写"你可以加好友"没用，代码里就没有这个动作。

---

## 探索模式

打开后机器人会主动探测 QQ/NapCat 到底支持哪些能力。

- 只调用 `get_` 和查询类接口：账号、群、成员、消息、群文件、公告、精华等；
- 不会踢人、发消息、改资料、支付；
- 后台每 `EXPLORE_INTERVAL_MINUTES` 分钟跑一轮，也可以点"立即探索"或在群里发 `/探索 跑`；
- 每次结果写进发现记录，列表里能看动作名、状态、返回摘要；
- 未知写操作即使 NapCat 报出来了也不会执行。

一般只在"想看看这个版本 NapCat 多了什么接口"时开，平时关着。

---

## 大脑

长期资料库的编辑器。三个区：范围树、笔记列表、正文编辑。

- **范围**：`global`（全局）/ 某个群 / 某个私聊。聊天时只会检索"当前范围 + 全局"，别的群的笔记不会串过去。
- **可见性**：`owner` 的笔记只给你看，不自动注入聊天。
- **新建 / 编辑 / 删除**：保存后立刻生效，不用重启。
- **存储占用**：能看到总条数和文件大小，接近 `BRAIN_MAX_NOTES` 时该清理了。

正文是 Markdown 文件（`data/brain/notes/`），SQLite 只存索引。**别在机器人运行时手动改这些文件**，索引会和正文对不上。

---

## 主页与空间

### 主页

- 生成草稿：按当前人设 + 全局大脑生成昵称/签名/简介/标签；
- 应用：推送到 QQ（调 NapCat 的 `set_qq_profile`）；
- 手动编辑：直接改字段，保存时同样过内容校验并备份版本；
- 历史回滚：每次应用前自动存一版，选一个点"恢复"即可。

第一次用建议顺序：**生成 → 看草稿 → 应用 → 去 QQ 里确认**。确认没问题再考虑把 `PROFILE_AUTO_ENABLED=true`。

### QQ 空间

- 生成 / 发布 / 删除动态，图片最多 9 张；
- 实时列表：读当前登录 QQ 的动态流（需要 `QZONE_WEB_ENABLED=true`）；
- 点赞 / 取消赞 / 评论 / 删自己的评论；
- 发布频率受限：每天最多 `QZONE_MAX_PER_DAY` 条，`QZONE_QUIET_START`~`QZONE_QUIET_END` 不发，评论每小时上限 `QZONE_COMMENT_HOURLY_LIMIT`。

发布前的内容过滤（广告、诈骗、支付、链接、二维码、长串数字）在控制台手动发也会生效，不是只挡自动发布。

---

## QQ 频道

- **后端状态**：`auto` / `plugin` / `official` / `off`，以及当前实际走的是哪条；
- **配置**：填 `guild_id` + `channel_id` 保存，等价于 `/频道 设置`；
- **检测**：检查同账号插件是否加载成功、官方机器人凭证是否可用；
- **发送**：选子频道发一条测试消息；
- **频道列表 / 最近消息**：确认发出去的东西长什么样。

插件不可用时会自动回退官方频道机器人（前提是配了 `GUILD_BOT_APPID` / `GUILD_BOT_SECRET`）。官方机器人发出来的是机器人身份，不是你的 QQ 号——这是正常的。

---

## 表情库

- 上传：支持常见图片格式，入库时自动算 hash 去重；
- 预览：直接看图和标签；
- 改标签 / 描述：标签影响聊天时的检索命中率，建议写"什么情绪下用"；
- 启停：停用后不会被选中，但文件还在；
- 删除：同时删文件和数据库记录。

`STICKER_AUTO_TAG=false` 时新表情没有标签，需要在这里手动补，否则很难被检索到。

---

## 风格学习

按范围展示画像：

- **预览画像**：看它学到了什么（长度、节奏、语气、标点、emoji）；
- **立即学习**：手动触发一次，至少要 5 条可用样本；
- **启用 / 停用**：停用后这份画像不再注入；
- **重置**：清空重新学；
- **历史版本 / 回滚**：每次更新存一版，学歪了可以退回去。

下面的"风格进化"区还有三块：进化日志（每次演进写了什么）、反馈信号（对方有没有接话/追问/吐槽，只记行为不记原文）、去 AI 化命中统计。

调试建议：先开 `DEAI_RECORD_ONLY=true` 观察几天命中分布，再调阈值、打开改写。

---

## 测试聊天

不经过 QQ，直接在这里和大肥鱼对话。

- 用的是当前人设、当前上下文配置、当前模型；
- 不写 QQ 会话，不发给任何人；
- 但**会消耗 token**，也会写进统计。

用来验证"改了人设/风格之后语气对不对"，比拉个群试快得多。

---

## 记忆管理

列出长期记忆（按范围、按用户），可以单条删除。

- 只显示摘要，不显示完整原文；
- 删除是不可恢复的，用户发 `/忘记我` 走的是同一个删除路径；
- 想彻底不记：`.env` 里 `MEMORY_ENABLED=false`。

---

## 人设编辑

- 直接编辑 YAML，保存后**热重载**，不用重启；
- 保存前会做结构校验，字段写错会拒绝并提示；
- 每次保存自动备份到 `data/persona_history/`；
- 历史版本可以一键回滚。

`display_name` 和 `aliases` 同时是"叫名字触发"的词，改完记得同步 `.env` 的 `NICKNAME`。

---

## 群聊开关

列出所有已知群：

- 开启 / 关闭（等价于群里的 `/关闭` `/开启`）；
- 智能 / 全量模式切换；
- 重置上下文（清空这个群的短期会话，不动长期记忆）。

大群别开全量。

---

## 错误面板与重启

- **错误面板**：最近的后台异常，带时间、来源、简短堆栈。排查"为什么没回消息"先看这里。
- **一键重启**：重启 Bot 进程。重启期间 NapCat 连接会短暂断开，几秒后自动重连；不会重新扫码。

重启只重启 Python 进程，不动 NapCat。

---

## API 端点

控制台所有操作都走 HTTP API，鉴权用 `ADMIN_TOKEN`（请求头 `X-Admin-Token`，或 `?token=`）。想写脚本自动化时可以直接调。

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/admin` | 控制台页面 |
| GET | `/admin/api/status` | 运行状态总览 |
| GET | `/admin/api/usage` | token 用量 |
| GET / PUT | `/admin/api/autonomy` | 自主权限读写 |
| GET / PUT | `/admin/api/explore` | 探索模式读写 |
| POST | `/admin/api/explore/run` | 立即探索一轮 |
| GET / PATCH | `/admin/api/skills` | 技能列表 / 改技能 |
| PUT / DELETE | `/admin/api/skills/grants/{scope_type}/{scope_id}/{skill_id}` | 范围覆盖增删 |
| GET | `/admin/api/skills/audit` | 最近审计 |
| GET | `/admin/api/skills/bot-role` | 机器人在各群的身份 |
| GET / POST | `/admin/api/brain` | 笔记列表 / 新建 |
| GET / PATCH / DELETE | `/admin/api/brain/{note_id}` | 单条读写删 |
| GET | `/admin/api/presence` | 主页与空间状态 |
| POST | `/admin/api/presence/profile/optimize` | 生成主页草稿 |
| POST | `/admin/api/presence/profile/apply` | 应用主页 |
| POST | `/admin/api/presence/profile/restore` | 回滚主页 |
| POST | `/admin/api/presence/qzone/generate` | 生成空间文案 |
| POST | `/admin/api/presence/qzone/post` | 发布空间动态 |
| DELETE | `/admin/api/presence/qzone/{tid}` | 删除动态 |
| GET | `/admin/api/qzone` | 实时空间列表 |
| POST | `/admin/api/qzone/like` | 点赞 / 取消赞 |
| POST | `/admin/api/qzone/comment` | 评论 |
| POST | `/admin/api/qzone/comment/delete` | 删自己的评论 |
| GET | `/admin/api/guild` | 频道状态 |
| POST | `/admin/api/guild/check` | 检测后端 |
| PUT | `/admin/api/guild/config` | 保存 guild/channel |
| GET | `/admin/api/guild/channels` | 子频道列表 |
| POST | `/admin/api/guild/send` | 发频道消息 |
| GET | `/admin/api/guild/messages` | 频道最近消息 |
| GET / POST | `/admin/api/stickers` | 表情列表 / 上传 |
| GET | `/admin/api/stickers/{id}/image` | 取表情图 |
| PATCH / DELETE | `/admin/api/stickers/{id}` | 改标签 / 删除 |
| GET / POST | `/admin/api/styles` | 画像列表 / 学习 |
| GET / PATCH / DELETE | `/admin/api/styles/{scope_type}/{scope_id}` | 单份画像 |
| GET | `/admin/api/styles/{scope_type}/{scope_id}/versions` | 画像历史 |
| POST | `/admin/api/styles/.../versions/{version}/restore` | 回滚画像 |
| GET / PUT | `/admin/api/persona` | 人设读写 |
| GET | `/admin/api/persona/versions` | 人设历史 |
| POST | `/admin/api/persona/restore/{name}` | 回滚人设 |
| GET / DELETE | `/admin/api/memories` | 记忆列表 / 删除 |
| POST | `/admin/api/test-chat` | 测试聊天 |
| POST | `/admin/api/groups/{group_id}` | 群开关 / 模式 |
| POST | `/admin/api/groups/{group_id}/reset` | 重置上下文 |
| GET / POST | `/admin/api/actions` | 能力表 / 执行动作 |
| GET | `/admin/api/errors` | 最近错误 |
| POST | `/admin/api/restart` | 重启进程 |
| GET | `/admin/api/style/evolution` | 进化日志 |
| GET | `/admin/api/style/feedback` | 反馈信号 |
| GET | `/admin/api/style/deai` | 去 AI 化命中 |
| POST | `/admin/api/style/review` | 立即复盘 |
| PUT | `/admin/api/style/config` | 风格配置 |

---

## 控制台常见问题

**Q：打开是 404。**
`bot.py` 没跑起来，或者 `PORT` 被改了。看启动窗口有没有报错。

**Q：输入 token 一直提示错误。**
改了 `.env` 但没重启；或者 `ADMIN_TOKEN` 里有空格/引号。值不要加引号。

**Q：页面数据不刷新。**
控制台是点开面板时才拉数据的。切一下标签页，或者刷新页面。

**Q：点了"应用主页"报错。**
多半是 NapCat 的 `set_qq_profile` 不可用（版本差异）。先用 `/昵称` `/签名` 单独试，能通再回控制台应用。

**Q：点了重启没反应 / 页面卡住。**
重启会断开当前页面请求，几秒后刷新页面即可。如果进程真的没起来，看启动窗口的报错。

**Q：能远程访问吗？**
不建议。控制台只绑 `127.0.0.1`，就是为了不暴露。真需要远程，用 SSH 隧道：

```bash
ssh -L 8080:127.0.0.1:8080 user@your-server
```

然后本地开 `http://127.0.0.1:8080/admin`。**不要**把 8080 直接映射到公网。

---

## 安全提醒

- `ADMIN_TOKEN` 等于这台机器人的全部权限，别复用其他密码，别截图，别提交 Git。
- 控制台的操作都会写技能审计，包括你自己的。
- 借别人电脑演示完，记得在登录页退出，清掉浏览器记住的 token。
