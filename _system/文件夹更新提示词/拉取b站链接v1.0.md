# Role & Task: 构建「B站收藏夹 ➔ Obsidian 双向同步与 AI 策展系统」

你现在处于我的 Obsidian 知识库根目录（`master`）。请先扫描当前目录结构（包含 `00.全局与方法` 到 `10.转写结果`、`scripts/`、`任务.md`、`AGENTS.md` 等），然后严格按照以下工程规范，帮我编写代码并创建对应的文件结构。

---

## 一、 需要在仓库中创建/更新的目录与文件

请直接在当前工作区创建以下目录和文件：

1. **新建主目录：** `11.灵感与收藏库/`（与 `10.转写结果` 平级）
   - 在其下创建预设分类子目录：
     - `01_考研408与数学/`
     - `02_AI与Agent工作流/`
     - `03_学校课业与开发/`
     - `04_效率与认知成长/`
     - `99_电子榨菜与待定/`
     - `_已取消收藏回收站/`
2. **新建同步与分类脚本：** `scripts/bili_sync.py` 及配置文件 `scripts/.env.example`（并在 `scripts/requirements.txt` 写入依赖：`requests`, `openai`, `pyyaml`, `python-dotenv`, `flask`, `flask-cors`）。
3. **新建浏览器油猴脚本：** `scripts/bili_patrol.user.js`（供我在浏览器 Tampermonkey 中安装，用于在 B 站页面一键触发同步）。
4. **更新/追加 Agent 规则：** 将本文档【第四部分】的交互指令写入 `11.灵感与收藏库/00_管家指令集.md`（供我后续在 Obsidian 中使用 Claudian 插件时调用）。

---

## 二、 核心脚本需求：`scripts/bili_sync.py`

请编写一个结构清晰、健壮的 Python 脚本，实现以下功能：

### 1. 配置读取（通过 `scripts/.env`）
- `BILI_UID`: 用户的 B 站 UID
- `BILI_SESSDATA`: （可选）用于读取私密收藏夹的 Cookie
- `DEEPSEEK_API_KEY`: DeepSeek 官方 API Key
- `DEEPSEEK_BASE_URL`: 默认为 `https://api.deepseek.com`
- `VAULT_DIR`: 默认自动定位到当前 Obsidian 仓库根目录

### 2. B 站收藏夹拉取逻辑
- 调用 B 站 API 获取用户所有收藏夹列表（`https://api.bilibili.com/x/v3/fav/folder/created/list-all`）。
- 遍历每个收藏夹，分页拉取里面所有视频的元数据（`https://api.bilibili.com/x/v3/fav/resource/list`），提取：`bvid`、`title`、`intro`（简介）、`upper.name`（UP主）、`duration`（秒转分钟）、所属原收藏夹名称。
- 自动跳过已失效（标题为“已失效视频”）的内容。

### 3. 本地状态扫描与「双向 Diff + 保护锁」机制（核心痛点）
每次运行脚本时，先扫描 `11.灵感与收藏库/`（除 `_已取消收藏回收站/` 外）下所有的 `.md` 文件，解析其 YAML Frontmatter：
- **自动检测用户手动微调（Auto-Lock）：**
  如果某个本地 `.md` 文件当前所在的父文件夹名称，与它 YAML 中记录的 `ai_category` 不一致，说明用户在 Obsidian 里手动拖拽微调过分类！此时脚本必须：
  1. 将其 YAML 中的 `ai_category` 更新为当前实际所在的文件夹名；
  2. 将 `user_locked` 强制设为 `true` 并保存。此后 AI 永远不得覆盖该文件的分类。
- **增量新增（云端有，本地无 `bvid`）：**
  调用 DeepSeek API（使用 `deepseek-chat` 模型，开启 JSON Mode）进行自动分类与指导语生成，并在对应分类文件夹下生成 `.md` 笔记。
- **删除同步（本地有 `bvid`，但云端所有收藏夹中均已不存在）：**
  检查该 `.md` 文件正文中 `## 💡 我的学习/折腾笔记` 标题下方是否有用户手写的非空内容：
  - **若无手写内容：** 直接将该文件移动至 `11.灵感与收藏库/_已取消收藏回收站/`（或物理删除），保持目录干净。
  - **若有手写内容：** 保留文件原位置不动，仅将其 YAML 中的 `status` 修改为 `"已取消收藏(保留笔记)"`。

### 4. DeepSeek API 初筛分类 Prompt 规范
调用 DeepSeek API 时，System Prompt 如下，要求返回严格 JSON：
```text
你是一个 B 站收藏视频分类与学习指导引擎。当前用户的背景是：正在备考计算机考研（数学二 + 408：数据结构、计算机组成原理、操作系统、计算机网络），同时有大学课程作业（Web开发、人机交互、软件需求分析）以及研究 AI Agent 工作流的爱好。
请根据输入的视频标题、简介、UP主、时长，输出以下 JSON 格式：
{
  "ai_category": "只能从 [01_考研408与数学, 02_AI与Agent工作流, 03_学校课业与开发, 04_效率与认知成长, 99_电子榨菜与待定] 中五选一",
  "sub_tag": "具体细分标签，如：计组、数据结构、Agent、前端、娱乐等",
  "cognitive_load": "只能从 [🔴深度, 🟡平稳, 🟢低耗] 中三选一",
  "ai_guide": "用一句大白话说明：这视频核心讲了啥？最适合我在什么精神状态下看（如：适合写计组实验报告卡壳时看10分钟，或累了当榨菜看）？",
  "link_to_vault": "如果与考研科目相关，输出双链如 [[04.数据结构]] 或 [[05.计算机组成原理]]，否则输出空字符串"
}
```

### 5. 生成的单篇视频 Markdown 笔记格式
文件名需过滤非法字符（格式：`{title}_{bvid}.md`），内容模板如下：
```markdown
---
bvid: "{bvid}"
title: "{title}"
up: "{upper_name}"
duration: "{duration_min} 分钟"
url: "[https://www.bilibili.com/video/](https://www.bilibili.com/video/){bvid}"
bili_folder: "{original_fav_folder}"
ai_category: "{ai_category}"
sub_tag: "{sub_tag}"
cognitive_load: "{cognitive_load}"
status: "未看"
user_locked: false
sync_date: "{YYYY-MM-DD}"
---

# 📺 {title}

> [!tip] 🤖 DeepSeek 食用指南
> **建议场景：** {ai_guide}
> **关联知识库：** {link_to_vault} | **原收藏夹：** `{original_fav_folder}` | **时长：** {duration_min} 分钟
> 🔗 [点击直达 B 站观看](https://www.bilibili.com/video/{bvid})

## 📝 视频简介
{intro}

---
## 💡 我的学习/折腾笔记
```

### 6. 自动生成全局索引表：`11.灵感与收藏库/00_收藏夹全景索引.md`
每次同步结束时，自动汇总当前所有有效视频笔记，生成一份极简的 Markdown 统计与表格索引（包含：各分类文件夹数量统计、若某文件夹 > 20 条则标红提醒臃肿、以及所有 `status: 未看` 视频的简表：`[分类 | 认知负荷 | 时长 | 标题双链 | AI一句话指导]`）。
*目的：让后续 Obsidian 里的 Claudian 只需读取这 1 个文件，就能瞬间掌握整个收藏夹全局，极大节省 Token。*

### 7. 支持两种运行模式
- `python scripts/bili_sync.py`：直接执行一次完整同步。
- `python scripts/bili_sync.py --serve`：在本地启动一个极简的 Flask HTTP 服务（端口 `5055`），提供 `POST /sync` 接口，供浏览器油猴脚本一键调用并返回同步结果摘要。

---

## 三、 浏览器巡逻兵油猴脚本：`scripts/bili_patrol.user.js`

请编写一个标准的 Tampermonkey 脚本：
- `@match` 匹配 `*://space.bilibili.com/*/favlist*` 和 `*://www.bilibili.com/video/*`。
- 在页面右下角固定悬浮一个精致的胶囊按钮：`⚡ 同步至 Obsidian`。
- 点击后向 `http://127.0.0.1:5055/sync` 发送请求，按钮显示 `🔄 同步与 AI 分类中...`。
- 同步完成后，弹窗或 Toast 显示本次结果：`新增 X 个视频，清理 Y 个已取消收藏视频，发现 Z 个臃肿目录`。

---

## 四、 写入给 Claudian 使用的指令文件：`11.灵感与收藏库/00_管家指令集.md`

请将以下内容原样写入 `11.灵感与收藏库/00_管家指令集.md`，以便我在 Obsidian 中用 Claudian 插件对话时调用：

```markdown
# Obsidian 收藏夹管家 & 精力协同导师（Claudian 专用指令）

当你被我唤醒时，请先读取 `11.灵感与收藏库/00_收藏夹全景索引.md` 和根目录下的 `任务.md`。

## 场景 1：当我要求「整理收藏夹 / 微调分组」时
1. 检查 `00_收藏夹全景索引.md` 中是否有超过 20 个视频的臃肿目录，或分类不合理的视频。
2. **铁律：** 凡是笔记 YAML 中 `user_locked: true` 的文件，代表我已经手动锁定，绝对禁止移动。
3. 结合我的主线（数二、408四门课、Web/人机交互课业、AI工作流），先向我输出一份【目录拆分或文件移动建议预览表】，等我回复同意或提出修改意见后，你再实际移动文件并将调整后的文件设为 `user_locked: true`。

## 场景 2：当我表示「有点懈怠 / 不知道干啥 / 有空余时间」时
不要直接罗列任务清单，严格按以下两步与我对话：
1. **第一步（把脉提问）：** 结合 `任务.md` 和我的近况，用朋友聊天的自然语气只问我 **1 个简短的问题**，确认我当下的疲惫类型（例如：“今天是被 408 和高数耗干了逻辑脑，还是单纯觉得学校作业太无聊？现在想看个 10 分钟的 AI 视频换换脑子，还是顺手清掉一个 5 分钟的简单待办？”）。
2. **第二步（唯一推荐）：** 等我回答后，根据我的真实状态，从 `任务.md` 或 `00_收藏夹全景索引.md` 中挑出 **唯一 1 个最匹配的事**（比如推荐 1 个 `🟢低耗` 或 `🟡平稳` 的收藏视频并告诉我为什么现在看它最合适，或者指导我花 5 分钟做完一个低阻力学校任务）。
```

---

## 五、 执行要求
1. 请现在直接开始创建上述文件夹和文件，写出完整可运行的代码，不要使用伪代码或省略号。
2. 写完后，在终端打印出简明的「启动指南」（告诉我如何填入 `BILI_UID` 和 `DEEPSEEK_API_KEY`，并测试运行第一次同步）。