# QQ AI Bot（大肥鱼）

一个用 **任意 OpenAI 兼容模型服务（内置 12 家任选）+ NoneBot2 + NapCatQQ** 搭起来的 QQ 聊天机器人。

不是那种"你问一句它答一句"的问答机：它会自己判断该不该接话、用多长的句子回、要不要分条、配不配表情包，还有长期记忆、风格学习和一个能自己动手管群的技能系统。

> **免责声明**：NapCatQQ 属于非官方 QQ 协议实现，存在账号风控风险。请使用**单独的小号**，不要用主号。
> 不要把机器人用于骚扰、刷屏、诈骗或任何违法内容。使用即表示你了解并自担上述风险。

> **想最快跑起来？** 点右上角绿色的 **Code** 按钮 → **Download ZIP**，解压后双击 **`一键启动.bat`**。
> 不用装 git、不用敲命令行——脚本会自己检测 Python、装依赖、问你几个问题，然后启动。

---

## 目录

- [它能做什么](#它能做什么)
- [3 分钟跑起来](#3-分钟跑起来windows-双击版)
- [手动安装（进阶）](#手动安装进阶)
- [在 QQ 里使用](#在-qq-里使用)
- [功能详解](#功能详解)
- [本地网页控制台](#本地网页控制台)
- [测试](#测试)
- [常见问题](#常见问题)
- [安全边界](#安全边界)
- [项目结构](#项目结构)
- [License](#license)

---

## 它能做什么

**聊天**

- 好友私聊自动回复；群聊第一次 `@机器人` 激活，之后不用每条都 @。
- 智能接话：被 @、被叫名字、被回复、或者同一个人 90 秒内继续说话时才接，其余时间安静听着（只记上下文）。100 人群不会刷屏。
- 短期上下文 + 自动摘要 + 长期记忆（SQLite）；敏感信息（密码、身份证、银行卡、验证码、住址等）不写入。
- 图片理解：把图片和文字一起发给它。
- 有意图的分条回复：1~3 条，由模型用换行决定，不按句号机械拆分。
- 表情包库：自动收藏别人发的表情/动画表情，视觉模型打标签，聊天中低频配图。
- 风格学习：观察当前群/私聊里其他人的表达方式，蒸馏成短画像，影响回复长度、节奏、语气；可预览、停用、回滚。
- 去 AI 化闸门：回复发出前检测"AI 腔"，超阈值改写一次；回复反馈采集 + 定期自我批评。

**干活**

- **群管理**：禁言/解禁/撤回/公告/改名片/踢人/全员禁言/设管理/改群名。
- **账号控制**：头像/昵称/签名/在线状态/点赞/戳一戳/群头像/群头衔/群签到/精华消息。
- **长期大脑**：`data/brain/` 文件 + SQLite 索引，手动笔记 + 自动沉淀，聊天时按范围检索注入（最多 700 字）。
- **自动主页**：生成昵称/签名/简介/标签并应用，带历史版本回滚。
- **QQ 空间**：生成/发布/删除动态；读实时列表、点赞、评论、删自己的评论。
- **QQ 频道**：同账号 NapCat 插件或 QQ 官方频道机器人，自动回退。
- **统一能力表**：140+ 个 OneBot/NapCat 动作统一注册进技能系统，`/功能` 可查，控制台可执行，走同一套权限与审计。
- **本地网页控制台**：token 消耗面板、技能权限矩阵、自主权限、探索模式、大脑、主页/空间、表情库、风格画像、测试聊天、记忆管理、人设热重载、一键重启。只绑定 `127.0.0.1`。

**不做什么**

- 不加/删好友，不支付/转账/发红包，不主动私聊，不在自己不是管理的群里主动群发。
- 不操作机器人自己、主人和当前群主。
- 暂不联网搜索（这一版没做，避免额外费用和延迟）。

---

## 3 分钟跑起来（Windows 双击版）

不需要会命令行。把代码下下来、装好 Python 之后，剩下的事都在几个 .bat 里。

### 第 0 步：把代码弄到本地

**最省事**：点本页右上角绿色的 **Code** → **Download ZIP**，解压到一个**路径不含中文**的位置，比如 `D:\qq-ai-bot`。

> 解压出来的文件夹一般叫 `qq-ai-bot-main`，进去能看到 `一键启动.bat`，那就是项目根目录。
> 别放桌面——桌面路径通常是 `C:\Users\你的名字\Desktop`，带中文，个别依赖在这里会报编码错。

**会用 git**：

```bash
git clone https://github.com/liqishuo-123/qq-ai-bot.git
```

### 第 1 步：装 Python

装过就跳过。没装也**不用自己去找**——双击「一键启动.bat」时它会用 winget 自动装 3.12。

想自己装：去 <https://www.python.org/downloads/>，安装时**务必勾选 `Add python.exe to PATH`**。

### 第 2 步：双击「一键启动.bat」

它会依次做这些事，全程自动：

| 步骤 | 它会做什么 | 需要你做什么 |
|---|---|---|
| 1 | 找 Python，找不到就用 winget 装 | 弹权限窗口时点「是」 |
| 2 | 建虚拟环境 `.venv` | 等 |
| 3 | 装依赖，直连失败自动换清华镜像 | 等，第一次 2~5 分钟 |
| 4 | 检查配置，缺了就叫你填 | **回答几个问题** |
| 5 | 找 NapCat | 没装就回车跳过 |
| 6 | 检查 3001 端口 | 无 |
| 7 | 启动机器人 | 看日志 |

第 4 步只会问你这些：

| 问题 | 怎么答 |
|---|---|
| 你的 QQ 号 | 你自己的号，用来当管理员（**不是**机器人号） |
| 机器人名字 | 直接回车就是「大肥鱼」 |
| 用哪家模型 | 12 家里按序号选一个，直接回车是 DeepSeek 官方 |
| 模型 API Key | 它会告诉你去哪拿；粘贴后回车。本地 Ollama 跳过 |
| 用哪个模型 | **自动拉这个 Key 能用的模型列表**，选序号即可 |
| 控制台密码 | 直接回车，自动生成一个给你 |

填完会打印一份配置摘要和控制台密码，**密码记一下**，打开控制台要用。
最后还会当场发一个很小的测试请求：看到 `[OK] 连接正常` 就说明这家服务真的能用了。

### 第 3 步：启动 NapCat 并扫码

双击 **「启动NapCat.bat」**：

1. 第一次会让你指一下 NapCat 装在哪（它会先在 `C:\NapCatQQ`、`D:\NapCatQQ` 这些常见位置自己找）；
2. 找到后记住路径，以后直接双击就行；
3. 顺便检查 NapCat 里的 WebSocket 配置，缺 3001 端口会问你要不要自动补上（会先备份原文件）；
4. 然后拉起 NapCat，弹权限窗口点「是」，用**机器人小号**扫码登录。

> 还没装 NapCat？去 <https://github.com/NapNeko/NapCatQQ/releases> 下载文件名带 `Shell` 的那个包，解压到 `D:\NapCatQQ` 之类好找的地方。

### 第 4 步：确认成功

回到「一键启动.bat」的窗口，看到这两行就是通了：

```text
[OK] 已连上 NapCat，机器人 QQ=12345678 —— 成功了，去群里 @ 它试试
[OK] 控制台地址：http://127.0.0.1:8080/admin
```

没看到就对照一下：

| 现象 | 原因 | 怎么办 |
|---|---|---|
| 一直刷 `Trying to reconnect...` | NapCat 没启动，或 3001 没配 | 双击「启动NapCat.bat」，看它的检查结果 |
| 报 `does not support websocket client connections` | 少了 `websockets` 依赖 | 删掉 `.venv` 文件夹，重新双击一键启动 |
| 连上了但发消息没反应 | 群没激活，或 Key 无效 | 群里 @ 它一次；或双击「配置向导.bat」重填 Key |
| 控制台能打开但全是空白/报错 | 密码没填或填错 | 双击「配置向导.bat」看 ADMIN_TOKEN |

### 三个会被反复用到的文件

| 文件 | 什么时候用 |
|---|---|
| `一键启动.bat` | 每次开机器人 |
| `启动NapCat.bat` | 每次开 NapCat（顺序：先 NapCat，后机器人） |
| `配置向导.bat` | 换模型服务商、换 Key、换 QQ 号、改名字、忘了控制台密码 |

---

## 手动安装（进阶）

给 macOS / Linux 用户，以及想自己控制每一步的人。用「一键启动.bat」的话这一整节都可以跳过。

Windows 命令行：

```powershell
git clone https://github.com/liqishuo-123/qq-ai-bot.git
cd qq-ai-bot
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe scripts\setup_wizard.py   # 交互式填配置，也可以直接改 .env
.\.venv\Scripts\python.exe bot.py
```

macOS / Linux：

```bash
git clone https://github.com/liqishuo-123/qq-ai-bot.git
cd qq-ai-bot
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env
./.venv/bin/python scripts/setup_wizard.py
./.venv/bin/python bot.py
```

或者 `bash install.sh` 装好，`bash start.sh` 启动。

看到这两行就说明核心起来了（后面还要等 NapCat 连上）：

```text
Succeeded to load plugin "chat" from "src.plugins.chat"
QQ AI Bot ready. persona=大肥鱼 provider=DeepSeek 官方 model=deepseek-flash
```

---

### 配置 `.env`（详解）

> 用「一键启动.bat」时这一段已经自动做好了，下面是原理和手动改法。
>
> `.env` 已在 `.gitignore` 里，**永远不要提交**。最少改这四行：

```dotenv
SUPERUSERS=["你的QQ号"]          # 你自己（不是机器人号），拥有最高权限
NICKNAME=["大肥鱼"]              # 机器人名字，也是"叫名字触发"的触发词
LLM_API_KEY=sk-xxxxxxxx          # 模型 API Key
ADMIN_TOKEN=随便设一个本地口令     # 控制台登录用，别泄露
```

模型相关的另外几项由向导自动写好，一般不用碰。

### 模型服务（12 家任选一家）

`.env` 里就是这五行：

```dotenv
LLM_PROVIDER=deepseek            # 用哪家，决定请求按哪种方言拼
LLM_API_KEY=sk-xxxxxxxx
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-flash
LLM_TIMEOUT=60
```

**换服务商不用手改，双击「配置向导.bat」重选一家就行。** 它会把地址、模型、Key 一起换掉，并当场测一次连接。

| 服务商 | `LLM_PROVIDER` | `LLM_BASE_URL` | 去哪里拿 Key |
|---|---|---|---|
| 阿里云百炼 | `dashscope` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | bailian.console.aliyun.com |
| DeepSeek 官方 | `deepseek` | `https://api.deepseek.com` | platform.deepseek.com/api_keys |
| 硅基流动 | `siliconflow` | `https://api.siliconflow.cn/v1` | cloud.siliconflow.cn（一个 Key 通吃多家模型） |
| 智谱 GLM | `zhipu` | `https://open.bigmodel.cn/api/paas/v4` | bigmodel.cn |
| 月之暗面 Kimi | `moonshot` | `https://api.moonshot.cn/v1` | platform.moonshot.cn |
| 火山方舟（豆包） | `ark` | `https://ark.cn-beijing.volces.com/api/v3` | console.volcengine.com/ark |
| 腾讯混元 | `hunyuan` | `https://api.hunyuan.cloud.tencent.com/v1` | console.cloud.tencent.com |
| MiniMax | `minimax` | `https://api.minimaxi.com/v1` | platform.minimaxi.com |
| OpenAI | `openai` | `https://api.openai.com/v1` | platform.openai.com/api-keys |
| Google Gemini | `gemini` | `https://generativelanguage.googleapis.com/v1beta/openai` | aistudio.google.com/apikey（有免费额度） |
| 本地 Ollama | `ollama` | `http://127.0.0.1:11434/v1` | 不需要 Key |
| 自定义中转 | `custom` | 自己填，一般以 `/v1` 结尾 | 你的中转服务商 |

`LLM_MODEL` **不用背**：向导会调服务商的 `/models` 接口，把这个 Key 实际能用的模型列出来给你选。这比在文档里抄模型名可靠——厂家的模型换代很快，写死的名字半年就会过期。

手动改的时候注意 `LLM_BASE_URL` 要填完整：智谱是 `/v4` 结尾，Gemini 是 `/v1beta/openai`，不是所有家都简单地加 `/v1`。填错了向导会提示「接口地址不对」。

> **从 v0.1 升级？** 旧的 `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` 仍然照常读取，`.env` 一个字不用改。

> 图片理解和表情包自动打标签会调用**多模态模型**。如果你的模型不支持图片输入，把 `IMAGE_ENABLED=false`、`STICKER_AUTO_TAG=false` 关掉即可，其他功能不受影响。

### `.env` 里的连接参数

```dotenv
DRIVER=~fastapi+~websockets
ONEBOT_WS_URLS=["ws://127.0.0.1:3001"]
ONEBOT_ACCESS_TOKEN=          # NapCat 的 WS 设了 token 就填一样的
```

### 常用调优

```dotenv
MAX_OUTPUT_TOKENS=120         # 单条回复上限，越小越省钱
CONTEXT_MESSAGES=8            # 带多少条历史进上下文
REPLY_MAX_BUBBLES=3           # 分条上限（防刷屏，不是固定条数）
TEMPERATURE=0.9
THINKING=false                # 不要思考，省 token。有些模型关不掉，会自动忽略
DAILY_TOKEN_BUDGET=0          # 每日 token 上限，0 = 不限
TOKEN_WARN_RATIO=0.8          # 用到 80% 提醒一次
STREAM_REPLIES=false          # 保持整段生成再分条
```

**不要着急打开自动功能。** `PROFILE_AUTO_ENABLED` 和 `QZONE_AUTO_ENABLED` 默认都是 `false`，先在控制台手动验证 NapCat 的对应接口正常，再改成 `true`。

完整配置项都在 `.env.example` 里，每项都有中文注释。

---

### 手动配置 NapCat 的 WebSocket

> 用「启动NapCat.bat」时，它已经帮你检查过了，这一段只在自动配置失败时看。

推荐 **正向 WebSocket**（NapCat 开服务端，Bot 去连）：

1. 启动 NapCat，扫码登录**机器人小号**。
2. 打开 NapCat 的 WebUI（默认 `http://127.0.0.1:6099/webui`）。
3. 网络配置 → 新建 **WebSocket 服务器**：
   - Host `127.0.0.1`
   - Port `3001`
   - Token 可留空；设了就同步到 `.env` 的 `ONEBOT_ACCESS_TOKEN`
4. 启用并重启 NapCat。
5. 确认 `.env` 里是 `DRIVER=~fastapi+~websockets` 和 `ONEBOT_WS_URLS=["ws://127.0.0.1:3001"]`。

**正向不通就换反向**：NapCat 里建 **WebSocket 客户端**，地址 `ws://127.0.0.1:8080/onebot/v11/ws`；`.env` 改成 `DRIVER=~fastapi`，并把 `ONEBOT_WS_URLS=[]`（否则会同时尝试正向连接）。两种方式选一种。

> 机器人 QQ 必须在目标群里。不需要给管理员，但群管理命令要求它本身是管理员/群主。

---

### 启动命令

```powershell
# Windows
.\.venv\Scripts\python.exe bot.py
```

```bash
# macOS / Linux
./.venv/bin/python bot.py
```

第一次启动会在 `data/` 下自动创建 `bot.db`、`brain/`、`stickers/` 等运行时目录。

开机自启（Windows）：给 `一键启动.bat` 建个快捷方式，丢进 `shell:startup` 目录即可。想开机只起机器人、不弹配置向导，用 `start.bat`。Linux 用 systemd，参考：

```ini
[Unit]
Description=QQ AI Bot
After=network.target

[Service]
WorkingDirectory=/opt/qq-ai-bot
ExecStart=/opt/qq-ai-bot/.venv/bin/python bot.py
Restart=always
User=youruser

[Install]
WantedBy=multi-user.target
```

---

## 在 QQ 里使用

### 群聊

1. 把机器人拉进群。
2. 第一次 `@大肥鱼 在吗`，它会回一条激活提示。
3. 之后不用每条都 @：叫"大肥鱼/肥鱼"、回复它的消息、或者同一个人 90 秒内继续说，都会接。
4. 激活状态一直保持，直到有人发 `/关闭`。

### 私聊

直接发消息。默认开启；`/关闭` 后停止，`/开启` 恢复。

### 命令表

命令前缀默认 `/`，可在 `.env` 的 `COMMAND_START` 改。

| 命令 | 作用 | 权限 |
|---|---|---|
| `/帮助` | 命令列表 | 所有人 |
| `/状态` | 当前开启状态/模式 | 所有人 |
| `/关闭` / `/开启` | 关闭/开启本群或本私聊 | 群主/管理员/激活者；私聊本人 |
| `/模式 智能` | 只接 @、叫名字、回复、同人追问 | 同上 |
| `/模式 全量` | 每条消息都回（费 token，大群慎用） | 同上 |
| `/重置` | 清空当前会话上下文 | 同上 |
| `/人设` | 查看当前人设 | 所有人 |
| `/记住 <内容>` | 手动记一条 | 已激活范围内的用户 |
| `/记忆` / `/忘记我` | 查看记忆 / 删除关于你的记忆 | 同上 |
| `/插件` | 可选插件加载状态 | 仅 `SUPERUSERS` |
| `/技能` / `/功能` | 查看当前范围可用技能 / 全部能力 | 所有人 |
| `/探索 [跑]` | 探索状态；`跑` 探测一轮只读 QQ 能力 | 探索模式开启后所有人 |
| `/禁言 @人 10m` | 禁言；也可回复消息后直接发 | 机器人管理员 + 权限 |
| `/解禁 @人` | 解除禁言 | 同上 |
| `/撤回` | 撤回被回复的消息 | 同上 |
| `/公告 <内容>` | 发群公告 | 同上 |
| `/改名片 @人 <名片>` | 改群名片 | 同上 |
| `/踢 @人` | 踢人 | 同上 |
| `/全员禁言 开/关` | 全员禁言开关 | 同上 |
| `/设管理 @人 开/关` | 设置/取消管理员 | 机器人群主 |
| `/群名 <名字>` | 改群名 | 机器人管理员 + 权限 |
| `/大脑` | 查看当前范围笔记 | 所有人 |
| `/大脑 查 <词>` / `/大脑 看 <id>` | 检索 / 查看一条 | 按技能权限 |
| `/大脑 记 标题 \| 内容` | 写入当前范围 | 管理员以上 |
| `/大脑 删 <id>` | 删除资料 | 主人 |
| `/主页` / `/主页 优化` | 查看主页资料 / 生成并应用 | 所有人 / 主人 |
| `/主页 应用` `/主页 历史` `/主页 恢复 <版本>` | 应用、查看、回滚 | 主人 |
| `/发空间 [内容] [图片]` | 生成或发布空间动态，最多 9 图 | 主人 |
| `/空间` | 本地发布记录 | 主人/按技能 |
| `/空间 列表 [QQ]` | 读实时动态 | 主人/按技能 |
| `/空间 点赞 <tid> [unikey]` / `取消赞` | 点赞 / 取消赞 | 主人/按技能 |
| `/空间 评论 <tid> <内容>` / `删评论 <tid> <id>` | 评论 / 删自己的评论 | 主人/按技能 |
| `/删空间 <tid>` | 删除动态 | 主人 |
| `/频道 [状态]` | 频道后端、插件、官方机器人状态 | 所有人 |
| `/频道 列表` / `/频道 历史 [条数]` | 频道列表 / 最近消息 | 按技能 |
| `/频道 设置 <guild_id> <channel_id> [auto/plugin/official/off]` | 保存频道配置 | 主人 |
| `/频道 发送 [channel_id] <内容>` | 发频道消息 | 按技能 |
| `/头像 [图片]` | 改机器人头像，也可回复图片 | 主人 |
| `/昵称 <名字>` / `/签名 <签名>` | 单独改昵称 / 签名 | 主人 |
| `/在线 在线\|离开\|隐身\|忙碌\|Q我吧\|请勿打扰` | 改在线状态 | 主人 |
| `/点赞 @人 [次数]` | 给指定 QQ 点赞，最多 10 次 | 主人 |
| `/戳 @人` | 群里戳一戳 | 管理员以上 |
| `/群头像 [图片]` | 改当前群头像 | 机器人群管理 + 主人 |
| `/群头衔 @人 <头衔>` | 设/取消群专属头衔 | 机器人群管理 + 管理员以上 |
| `/群签到` | 替机器人完成群打卡 | 管理员以上 |
| `/精华` / `/取消精华` | 回复消息后设/取消群精华 | 机器人群管理 + 管理员以上 |

> 这些命令是"请求"而不一定执行：默认配置下，大肥鱼会自己判断接不接，可以在回复里直接拒绝。控制台"自主权限"面板的开关和指令优先级最高。

---

## 功能详解

### 智能接话规则

群聊激活后，`智能` 模式下满足任一条件才回复：

- 消息 @ 了机器人；
- 文本里出现"大肥鱼 / 肥鱼"等名字（`aliases` 可配）；
- 这条消息是回复机器人某条消息；
- 同一个人在上次机器人回复他之后的 `GROUP_FOLLOWUP_WINDOW` 秒内继续说话（默认 90 秒）。

其他消息只记录上下文，不回复。`全量` 模式回复所有非命令消息，适合小群测试。

### 记忆与图片

- 每积累 `MEMORY_EXTRACT_EVERY` 条消息，后台抽取一次稳定信息（称呼、偏好、重要经历、约定）。
- 私聊记忆只在私聊用，群聊记忆只在该群用，按用户区分，避免串群泄露；默认 90 天过期。
- 发图片时下载第一张图，转 data URL 交给多模态模型理解；历史里只留 `[图片]` 占位；默认 8 MB 上限。
- 默认禁止图片 URL 指向本机/内网（防 SSRF）。只有 NapCat 返回内网图片地址时才把 `IMAGE_ALLOW_PRIVATE_HOSTS=true`。

### 表情包库

- 只自动收藏别人的**表情/动画表情**（`image.sub_type != 0` 或 `summary` 含 `[动画表情]`），普通照片不收。
- 存在 `data/stickers/`，元数据在 SQLite，按 sha256 去重；异步打标签，不阻塞聊天。
- 聊天中由模型自己判断配不配图，有随机概率、冷却和每小时/每天上限；附在最后一条气泡里，不多刷一条。
- 控制台可以上传、预览、改标签、启停、删除。

```dotenv
STICKER_LIBRARY_ENABLED=true
STICKER_INGEST_ENABLED=true
STICKER_AUTO_TAG=true
STICKER_AUTO_SEND=true
STICKER_AUTO_CHANCE=0.35
STICKER_COOLDOWN_SECONDS=300
STICKER_HOURLY_LIMIT=4
STICKER_DAILY_LIMIT=12
```

### 风格学习

不是训练模型权重，而是把当前聊天环境里其他人的表达习惯蒸馏成一份短画像，作为"语感参考"放进系统提示词。核心人设和安全规则始终优先。

- 群画像只在该群生效，私聊画像只在对应私聊生效，不跨范围共享。
- 只学长度、节奏、语气、标点、emoji、互动方式；不保存姓名、QQ 号、链接、事实和隐私。
- 达到最小样本数和置信度才注入，默认最多 `STYLE_INJECT_MAX_CHARS` 字符。
- 控制台可预览、立即学习、启停、重置、回滚历史版本。

```dotenv
STYLE_LEARNING_ENABLED=true
STYLE_AUTO_APPLY=true
STYLE_LEARN_EVERY=50
STYLE_MIN_SAMPLES=20
STYLE_MAX_SAMPLES=120
STYLE_MIN_CONFIDENCE=0.35
STYLE_INJECT_MAX_CHARS=700
```

> 风格学习会把采样到的群消息发给模型 API。已经过滤敏感信息，但如果你不想让未直接和机器人互动的群消息参与，设 `STYLE_LEARNING_ENABLED=false`。

### 技能权限与自主判断

- 角色从高到低：`owner`（`SUPERUSERS`）> `group_owner`（群主）> `admin`（群管理员）> `member`。
- 默认所有技能最低角色都是 `member`；控制台可以调高、关闭或按群/私聊覆盖，QQ 里不能改权限（避免聊天中被提权）。
- 开启"自主判断"后，每条有副作用的命令先让模型做一次 allow/deny；它可以拒绝并说明原因。
- 所有执行、拒绝、失败都写 `skill_audit`，控制台可查最近 100 条。
- **探索模式**：自主判断全放行，并定时只读探测 QQ 能力（只调 `get_`/查询类接口，不踢人不发消息）。

### 长期大脑

`data/brain/` 是长期资料库，正文在 `notes/*.md`，SQLite 只存索引：

- 手动 `/大脑 记 标题 | 内容`，或记忆抽取后自动沉淀短句。
- 聊天时只检索"当前范围 + 全局"，`visibility=owner` 的笔记不自动注入。
- 单条最大 `BRAIN_MAX_NOTE_BYTES`，最多 `BRAIN_MAX_NOTES` 条，路径固定，拒绝控制字符和提示词注入。

### 主页与 QQ 空间

- `/主页 优化` 生成昵称/签名/简介/标签，本地校验后应用；自动更新默认关闭。
- `/发空间` 支持 9 图以内，可见范围 `QZONE_UGC_RIGHT`：`1` 公开、`4` 好友、`64` 仅自己。
- 每天最多 `QZONE_MAX_PER_DAY` 条，夜间 `QZONE_QUIET_START`~`QZONE_QUIET_END` 不发，发布前过滤广告/诈骗/支付/链接/长串数字。
- 实时列表/点赞/评论需要 `QZONE_WEB_ENABLED=true`，用当前登录 QQ 的 H5 登录态（只缓存在内存，不写日志、不返回给前端）。

### QQ 频道

- 填 `GUILD_ID` / `GUILD_CHANNEL_ID`（或用 `/频道 设置`）。
- `GUILD_BACKEND=auto`：优先同账号 NapCat 插件 `qq-guild-bridge`；插件不可用且配了 `GUILD_BOT_APPID`/`GUILD_BOT_SECRET` 时改走官方频道机器人（显示的是机器人身份，不是登录的 QQ 号）。
- 插件是**可选**的：NapCat 只加载白名单插件，所以需要手动放行。

```powershell
# 先 dry-run 看路径对不对，再加 --apply
.\.venv\Scripts\python.exe scripts\install_napcat_guild_plugin.py --napcat-home "C:\NapCatQQ"
.\.venv\Scripts\python.exe scripts\install_napcat_guild_plugin.py --napcat-home "C:\NapCatQQ" --apply
```

---

## 本地网页控制台

启动 Bot 后浏览器打开：

```text
http://127.0.0.1:8080/admin
```

第一次要输入 `.env` 里的 `ADMIN_TOKEN`。

控制台的功能、每个面板怎么用、API 端点、以及出问题怎么排查，全部整理在独立文档里，避免和本 README 重复：

**→ [控制台使用说明 docs/CONSOLE.md](docs/CONSOLE.md)**

一句话版本：token 消耗、技能权限、自主权限、探索模式、大脑、主页/空间、频道、表情库、风格画像、测试聊天、记忆管理、人设热重载、错误面板、一键重启，都在这个页面里。

> 控制台只监听 `127.0.0.1`。**不要把 8080 映射到公网**，也不要把 `ADMIN_TOKEN` 截图发出去。

---

## 测试

```bash
./.venv/bin/python -m pytest -q          # 127 项单测
```

不接真实 QQ、不消耗 API 额度的端到端冒烟测试（起一个假的 NapCat WebSocket + 假的模型 HTTP 服务，跑完自动退出）：

```bash
python scripts/e2e_smoke.py
# 真实 NapCat 占着 3001 时：
E2E_WS_PORT=3101 python scripts/e2e_smoke.py
```

```powershell
# Windows
$env:E2E_WS_PORT='3101'
.\.venv\Scripts\python.exe scripts\e2e_smoke.py
```

覆盖：首次 @ 激活、叫名字触发、同人追问、普通群友不触发、命令权限、`/关闭` 后重新激活、私聊开关、分条、表情入库、技能权限、禁言/解禁、大脑写入检索、主页/空间、头像/在线状态/点赞/戳/群签到、自主拒绝、探索模式、控制台全部接口。

改完代码建议顺手跑一遍格式与静态检查：

```bash
./.venv/bin/python -m ruff check .
```

---

## 常见问题

**Q：日志说 `does not support websocket client connections`。**
`DRIVER` 改成 `~fastapi+~websockets`，确认 `websockets` 已安装；或者改用 NapCat 反向 WS。

**Q：Bot 在线，但群里 @ 没反应。**
依次查：NapCat 的 WS 连没连上、机器人 QQ 在不在群里、消息是否真的 @ 到它、`LLM_API_KEY` 有没有填对（双击「配置向导.bat」重测一次最快）、这个群是不是被 `/关闭` 了。看控制台的错误面板最快。

**Q：私聊没反应。**
先发 `/状态`，如果是"已关闭"，发 `/开启`。

**Q：报 401 / 402。**
401 是 Key 无效或过期，402 是余额不足。去服务商后台看。

**Q：装了但 pip 报 `File contains no section headers`。**
本机 pip 配置坏了，临时绕过：

```powershell
$env:PIP_CONFIG_FILE='NUL'
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**Q：会不会被封号？**
NapCat 不是官方协议，无法保证零风险。用小号、控制频率、别群发、别骚扰；如果是公开群，建议在群公告里说明这是 AI。

**Q：能联网搜索吗？**
这一版没有。搜索需要工具调用和额外 API 费用，也会增加延迟，等聊天稳定后再加。

**Q：想换人设 / 加个新人设？**
改 `config/personas/dafeiyu.yaml`（或用控制台直接编辑，保存后热重载），再把 `.env` 的 `PERSONA_FILE` 指过去。`display_name` 和 `aliases` 同时也是"叫名字触发"的词。

**Q：聊天记录和记忆会上传吗？**
本地 SQLite，不上传第三方。但对话内容会发给你在 `.env` 里配置的模型 API，请知情使用。

---

## 安全边界

- `.env` 已在 `.gitignore`；Key 只放本地，不要发群里、不要截图、不要提交 Git。
- 人设会拒绝隐私泄露和"忽略规则"这类提示注入；嘴臭有边界，不碰自杀自残、重大疾病、死亡、性暴力、未成年人。
- 代码级硬边界（不可配置）：不加/删好友、不支付/转账/红包、不主动私聊、不在非管理群主动群发、不操作机器人自己/主人/当前群主、不访问 `data/brain` 之外的任意路径。
- 控制台只绑 `127.0.0.1`，`ADMIN_TOKEN` 不要复用其他密码。
- 发现安全问题请按 [SECURITY.md](SECURITY.md) 私下反馈，别开 public issue。

---

## 项目结构

```text
qq-ai-bot/
├─ bot.py                     # 启动入口
├─ pyproject.toml
├─ requirements.txt
├─ .env.example               # 配置模板（复制为 .env）
├─ 一键启动.bat               # Windows 主入口：装环境 + 配置 + 启动
├─ 配置向导.bat               # 单独改配置（换服务商 / Key / QQ 号 / 密码）
├─ 启动NapCat.bat             # 找 NapCat、检查 WS 配置、启动
├─ install.bat / start.bat    # 兼容旧入口（install 会转调一键启动）
├─ install.sh / start.sh      # macOS / Linux
├─ config/
│  ├─ personas/dafeiyu.yaml   # 人设
│  └─ plugins.yaml            # 可选插件白名单
├─ src/
│  ├─ settings.py             # 配置
│  ├─ db.py                   # SQLite
│  ├─ persona.py              # 人设加载
│  ├─ llm.py                  # 模型客户端
│  ├─ context.py              # 上下文 / 摘要
│  ├─ memory.py               # 长期记忆
│  ├─ style.py                # 风格学习
│  ├─ safety.py               # 敏感信息与提示注入过滤
│  ├─ trigger.py              # 群聊接话判断
│  ├─ composer.py             # 回复清洗 / 分条
│  ├─ stickers.py             # 表情库
│  ├─ skills.py               # 技能注册、权限、审计
│  ├─ actions.py              # 统一能力表
│  ├─ group_admin.py          # 群管理
│  ├─ account.py              # 账号控制
│  ├─ policy.py               # 账号硬边界、公开内容过滤
│  ├─ brain.py                # 大脑
│  ├─ presence.py             # 主页与空间
│  ├─ qzone.py                # 空间 H5
│  ├─ guild.py                # QQ 频道
│  ├─ autonomy.py             # 自主判断
│  ├─ explore.py              # 探索模式
│  ├─ admin.py                # 控制台后端
│  ├─ admin.html              # 控制台页面
│  ├─ llm_presets.py          # 12 家模型服务商预设与各家 thinking 方言
│  └─ plugins/chat.py         # 核心聊天与命令
├─ napcat_plugins/
│  └─ qq-guild-bridge/        # 可选：同账号频道 action
├─ data/                      # 运行时生成，已 gitignore
├─ docs/CONSOLE.md            # 控制台使用说明
├─ scripts/                   # setup_wizard.py 配置向导、napcat_ws.py 配置检查、
│                             # check_config.py、e2e_smoke.py、插件安装脚本
└─ tests/
```

---

## 贡献

欢迎 issue 和 PR。提交前请：

1. `python -m ruff check .` 和 `python -m pytest -q` 都过；
2. 不要提交 `.env`、`data/` 下的数据库和图片、任何真实 QQ 号；
3. 新增能力时走 `src/skills.py` 的注册表，别绕过权限和审计。

细节见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## License

[MIT](LICENSE)。随便用、改、商用，出事自己担。
