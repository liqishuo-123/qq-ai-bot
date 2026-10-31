# 贡献指南

## 提交前

```bash
python -m ruff check .      # 静态检查
python -m pytest -q         # 127 项单测，必须全过
```

如果有真实 NapCat 在跑，冒烟测试换个端口：

```bash
E2E_WS_PORT=3101 python scripts/e2e_smoke.py
```

## 绝对不要提交

- `.env`、任何 API Key、`ADMIN_TOKEN`；
- 真实的 QQ 号（包括你自己的、机器人的、群里其他人的）；
- `data/` 下的数据库、表情图片、大脑笔记、人设历史；
- `logs/`、`__pycache__/`、`.venv/`。

提交前自查一次：

```bash
git diff --cached | grep -nE "sk-[A-Za-z0-9]{16,}|[1-9][0-9]{8,10}"
```

命中就 `git reset` 掉再改。

## 代码约定

- Python 3.12+，`from __future__ import annotations`，行宽 120。
- 新增一个能被模型调用的动作时，走 `src/skills.py` 的注册表，不要直接调 OneBot API：
  - 注册项要写清 `id`、描述、参数、风险等级、默认最低角色；
  - 执行统一走 `src/actions.py` 的路由，自动带上权限检查和 `skill_audit` 审计；
  - 有副作用（改资料、禁言、发消息）的动作必须能在"自主判断"里被拒绝。
- 新增配置项：加进 `src/settings.py` 的 dataclass **和** `.env.example`，带中文注释。
- 涉及外部输入（图片 URL、文件名、笔记正文）的地方，先过 `src/safety.py` / `src/policy.py` 的过滤。
- 数据库改动要能在老库上平滑升级（`CREATE TABLE IF NOT EXISTS` + `ALTER TABLE` 容错），不能要求用户删库。

## 测试

- 单测放 `tests/`，文件名 `test_<模块>.py`，用 `pytest-asyncio`（`asyncio_mode = auto`）。
- 不依赖真实 QQ、真实 API Key；HTTP 用 `httpx.MockTransport` 或假服务器。
- 涉及资金/权限/安全边界的改动，必须补测试。

## PR

- 一个 PR 只做一件事，描述里写清"改了什么、为什么、怎么验证"。
- 新增功能请在 README 对应章节补一句说明；控制台相关改动补到 `docs/CONSOLE.md`。
- 别改 LICENSE、别加商业化入口、别引入需要付费才能用的硬依赖。

## 人设与内容

仓库里的"大肥鱼"只是示例人设。你可以改成任何风格，但请保留这些底线：不碰未成年人、自残自杀、重大疾病、性暴力；不做诈骗、广告、骚扰；不假装真人骗感情。
