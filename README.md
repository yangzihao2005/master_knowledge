# 📦 22408 考研全栈 AI + Obsidian 工作流

> **适用目标**：22408（政治 + 英语二 + 数学二 + 408 计算机学科专业基础）
> **硬件形态**：PC + 数位板
> **核心理念**：单窗口沉浸刷题、零阻力错题归档、AI 精准定位"思维分叉点"。拒绝在排版和搬运文本上浪费任何一秒！

这是一个可直接作为 Obsidian Vault 打开的**个人刷题基地**：数位板手写演草自动按题号归档、错题一键抽取入库、AI 读手稿+标准答案精批定位出错步骤、错题库加权随机抽题生成重测卷，形成完整的学习闭环。

---

## 🧭 目录结构

```text
📦 Obsidian_22408_Vault
 ┣ 📂 00.全局与方法/          # 8.择校择专业、10.学习方法汇总 合并收纳
 ┣ 📂 01.高数/  02.线代/      # 数二网课精简笔记 + 专项讲义题
 ┣ 📂 03.数二题库/张宇1000题/  # 题库 OCR .md（1.基础篇、2.强化篇、试题册目录.md）
 ┣ 📂 04.数据结构/ ~ 07.计算机网络/   # 408 四科：课程笔记 + 王道章节题
 ┣ 📂 08.错题与重测库/
 ┃  ┣ 📂数二错题/ 📂408错题/   # extract_mistakes.js 自动归档的 <笔记名>_错题集.md
 ┃  ┣ 📂抽测组卷/             # random_exam.js 生成的重测卷
 ┃  ┗ 📄错题测试.md           # 历史错题件
 ┣ 📂 09.答案库/              # 供 AI 比对的标准答案 / PDF
 ┣ 📂 10.转写结果/            # ASR 输出的网课粗切文本
 ┣ 📂 Ink/                    # 手写草稿（自动按「笔记名_题号」命名 .writing）
 ┣ 📂 media-lib/              # 网课截图、PDF++ 框选 PNG、视频音频（Obsidian 附件默认目录）
 ┣ 📂 scripts/                # QuickAdd 宏脚本（见下表）
 ┣ 📄 AGENTS.md               # AI 行为规则：错题批改 + 网课转写精讲
 ┣ 📄 README.md / 任务.md
 └ 📂 .obsidian/plugins/ink/  # 含魔改版 main.js 与整版本备份
```

---

## ✨ 核心能力

### 1️⃣ Ink 数位板画布 · 源码魔改（`.obsidian/plugins/ink/main.js`）

基于 [obsidian_ink v0.3.4](https://github.com/daledesilva/obsidian_ink)（tldraw 内嵌手写）二次修改：

| 能力 | 说明 |
|---|---|
| **自动命名** | 新建手写/绘画画布自动按「当前笔记名 + 光标上方最近标题」命名，标题会**浓缩为题号**（`### 05`→`05`、`### 1. 函数f(x)=…`→`1`、`### 2.3 极限`→`2.3`），强制存入 `Ink/`，重名自动加 `(2)`，异常自动回退时间戳命名 |
| **AI 可索引** | 正文插入保留原版 `<!-- ink: Ink/xxx.writing -->` 注释 + 嵌入块，AI 批改时按注释精确定位手稿 |
| **防卡顿** | 阅读视图嵌入块渲染为折叠条，点击才懒加载 tldraw 渲染 |

改动集中在 `createNewWritingFile` / `createNewDrawingFile`、`insertNewWritingFile` / `insertNewDrawingFile` 与新增的 `stripMarkdownForFilename` / `getNearestHeadingText` / `condenseHeadingToNumber` / `getNewNamedInkFilepath`，另保留原版阅读视图折叠补丁（`WritingEmbedWidget.onload`）。

**备份与回滚**（`plugins/ink/` 内）：`main.js.original-v0.3.4.bak`（官方原版）、`main.js.bak` 等（各阶段魔改版）；哪个能跑用哪个覆盖 `main.js` 即可。

### 2️⃣ 脚本（`scripts/`，全部经 QuickAdd 宏挂载为命令）

| 文件 | 用途 | 快捷键 | 依赖 |
|---|---|---|---|
| `extract_mistakes.js` | 扫描当前笔记所有带 `#错题` 的题块（标题或正文打标均可识别），整块（题干/截图/我的错误结果/Ink 画布）按科目分流追加到 `08.错题与重测库/数二错题\|408错题/<笔记名>_错题集.md`，底部预留 AI 占位符，原文 `#错题`→`#已归档错题`，自动去重 | `Ctrl+Shift+E` | QuickAdd |
| `random_exam.js` | 从错题库加权随机抽 10~15 题（`#顽固错题` 权重高、`#已攻克` 几乎不抽中），**只留题干与配图**（剔除手稿/解析/错误答案），生成重测卷到 `抽测组卷/` | `Ctrl+Shift+R` | QuickAdd |
| `run_asr.js` | QuickAdd 一键弹出终端调用下面的 ASR 批量转写，结果固化输出到 `10.转写结果/` | `Ctrl+Shift+A` | QuickAdd |
| `mimo_asr_batch.py` | 小米 MiMo ASR 批量转写：视频 1min 精度切片 → mp3 → 逐片识别 → 合并带时间戳文本；限速、重试、断点续传 | — | Python3 + requests + ffmpeg |

### 3️⃣ 插件底座与快捷键

| 插件 | 用途 | 关键配置 |
|---|---|---|
| Ink（魔改） | 数位板手写 | 插画布 `Alt+I` |
| PDF++ | 教材/题册精确框选截图 | 自动聚焦+自动粘贴、PNG 格式、`Ctrl+Shift+D` 复制嵌入链接 |
| Media Extended | 倍速看课、精细时间戳 | 截图 `Ctrl+Q` |
| QuickAdd | 挂载上述宏 | 三个命令均已注册 |
| Claudian（realclaudian） | Vault 内 AI 助手 | 读取 `AGENTS.md` 行为规则 |

---

## 🀄 工作流闭环

```
看网课 ──ASR──▶ 10.转写结果 ──AI 整理──▶ 01~07 一级精简文档
                                      │（Media Extended 靶向精看 + 截图）
                                      ▼
刷题单窗口 ──题号画布──▶ Ink/ 手稿 ──只对答案──▶ 标 #错题
                                      │ Ctrl+Shift+E
                                      ▼
08.错题与重测库/<科目>_错题集.md ──AI 批改（读 Ink 手稿 + 09.答案库）──▶ 思维分叉点 + 标准解法
                                      │ Ctrl+Shift+R（加权随机组卷，顽固错题优抽）
                                      ▼
重测卷二刷 ──▶ 秒杀→#已攻克｜仍错→#顽固错题 ──▶ 回到上一步
```

规则细节全部固化在 **`AGENTS.md`**（Claudian/AI 打开错题集即自动按规则批改；含"不碰题干/手稿、严禁把解析写进重测卷"的保护铁律）。

---

## 🚀 快速开始

1. **Clone / 下载本仓库**，用 Obsidian「打开本地文件夹作为仓库」指向本目录。
2. **信任并启用社区插件**（Ink、PDF++、Media Extended、QuickAdd、Claudian 已在 `.obsidian` 预配置）。
3. **ASR 环境准备**：
   ```bash
   pip install requests
   winget install Gyan.FFmpeg      # 或 choco install ffmpeg
   # Windows PowerShell（推荐环境变量方式注入 API Key）
   $env:MIMO_API_KEY = "sk-xxxx"   # 脚本会自动读取 MIMO_API_KEY
   ```
4. 按 `Ctrl+Shift+A` 试跑一次转写，然后按「工作流闭环」开跑。

---

## ⚠️ 发布 / 开源前必读（Privacy Checklist）

- ⛔ `scripts/mimo_asr_batch.py` 中**写明了一个真实 API Key**——发布前将第 59 行改为空串（只用 `MIMO_API_KEY` 环境变量），并去服务商后台**轮换/作废该 Key**。
- ⛔ `.obsidian/plugins/quickadd/data.json` 内的 AI provider 若配置了真实 Key（apiKey 字段）需清空。
- ⛔ 个人学习内容（错题笔记、转写稿、截图）属于私有资产，开源前建议把 `01~10` 数据目录与 `.trash/`、`.agents/`、`.claudian/` 移出仓库或加入 `.gitignore`。
- `_错题集.md`、`抽测组卷/` 由脚本生成，属运行时产物，建议同样 ignore。

建议的 `.gitignore` 起步内容：

```gitignore
.trash/
.agents/
.claudian/
.obsidian/workspace.json
.obsidian/plugins/*/data.json   # 如需分享插件配置请自行审查后放开
01.*/
02.*/
03.*/
04.*/
05.*/
06.*/
07.*/
08.*/
09.*/
10.*/
Ink/
media-lib/
```

（即：仓库只保留「模板骨架」——本 README、`AGENTS.md`、`scripts/`、`.obsidian/plugins/ink/` 魔改源码与说明。）

---

## 🧾 致谢

- [Ink](https://github.com/daledesilva/obsidian_ink) — Dale de Silva（本仓库 `main.js` 为其 v0.3.4 的二次修改版，版权归原作者）
- [tldraw](https://tldraw.dev)、[QuickAdd](https://quickadd.obsidian.guide) — Christian B. B. Houmann、PDF++ 等社区插件作者
- MiMo ASR 接口规范参考小米官方文档

## 📄 License

个人学习使用。第三方插件源码（`main.js` 等）遵循其原作者许可；仓库内其余原创脚本与文档采用 MIT。
