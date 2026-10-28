# Ink Plugin — 魔改版 (Collapsible Embed)

> 基于 [Obsidian Ink v0.3.4](https://github.com/daledesilva/obsidian_ink) by Dale de Silva
> 魔改目标：阅读视图下手写笔迹嵌入块默认折叠，点击展开/收起，提升长文档浏览性能

---

## 文件清单

| 文件 | 说明 |
|------|------|
| `main.js` | **已魔改的主代码** — 阅读视图 ink embed 可折叠 |
| `main.js.bak` | 官方 v0.3.4 原版（未修改，用于对比） |
| `styles.css` | 样式文件（未修改） |
| `manifest.json` | 插件清单 |
| `data.json` | 用户配置样例 |

## 魔改内容（唯一修改）

仅涉及 `WritingEmbedWidget` 类（对应 TypeScript 源码 `src/tldraw/writing/writing-embed.ts`），整个 diff 只有 **69 行**、**3 个 hunk**，全部在同个类中。

### 修改前后对比

| | 原版 (main.js.bak) | 魔改后 (main.js) |
|--|-------------------|-----------------|
| 构造函数 | — | 新增 `this._expanded = false`、`this._contentEl = null` |
| 新增方法 | — | `_renderTldraw()`、`_unloadTldraw()` |
| `onload()` | 直接读取 `.writing` → 创建 React root → 挂载 tldraw | 渲染折叠条 `🖊 文件名 [展开 ▸]` → 点击才懒加载 tldraw |
| `onunload()` | `this.root?.unmount()` | `this._unloadTldraw()` |

### 精确 diff

```diff
--- original (main.js.bak)
+++ patched (main.js)

 // 1) 构造函数新增两个字段
+this._expanded = false;
+this._contentEl = null;

 // 2) 新增两个辅助方法
+_renderTldraw() {
+  if (this.root) {
+    this.root.render(/* Provider */ ...pageData={this._pageData} );
+    applyCommonAncestorStyling(this._contentEl);
+  }
+}
+_unloadTldraw() {
+  if (this.root) { this.root.unmount(); this.root = null; }
+  this._contentEl.innerHTML = "";
+}

 // 3) onload() 重构
 const pageDataStr = await v.read(this.fileRef);
 const pageData = JSON.parse(pageDataStr);
-... 直接挂载 tldraw 到 this.el
+→ 渲染折叠条 → 点击时懒加载/卸载 tldraw

 // 4) onunload() 调整
-this.root?.unmount();
+this._unloadTldraw();
```

完整 diff 可以直接运行：
```bash
diff -u main.js.bak main.js
```

### ❗ 关于"插入画布文件名到 md 文件"

你说的这个功能我仔细核查了——它是 **Ink 插件的原版行为，不是魔改内容**。

当你执行 `Ink: New handwriting section` 命令时，`buildWritingEmbed()`（行 17076-17088）会生成以下嵌入代码插入 md 文件：

```markdown
<!-- ink: 2026.6.29 - 13.18pm.writing -->

```handwritten-ink
{
  "versionAtEmbed": "0.3.4",
  "filepath": "二战数学/错题图片/Ink/Writing/2026.6.29 - 13.18pm.writing"
}
```

- `<!-- ink: 文件名 -->` HTML comment 里带文件名
- JSON 的 `filepath` 字段存完整路径

这两者在原版中就已经存在，**不是你的魔改内容**。你的魔改只改了**阅读视图渲染**阶段（`onload()` → 折叠而不是直接显示画布），`buildWritingEmbed` 函数两个版本完全一致。

如果你想要额外在 md 文件里**以明文文本**插入画布名字（比如 `🖊 2026.6.29 - 13.18pm`），那算一个新需求，需要额外修改 `buildWritingEmbed()`。

## 安装方式

在目标 Obsidian vault 中：

```bash
# 1) 创建插件目录
mkdir -p <vault>/.obsidian/plugins/ink/

# 2) 复制文件
cp main.js manifest.json styles.css <vault>/.obsidian/plugins/ink/

# 3) 复制配置（可选）
cp data.json <vault>/.obsidian/plugins/ink/
```

然后在 Obsidian 中 **Settings → Community plugins → 重新加载**，启用 Ink。

如果已有 Ink 插件并想保留配置，只覆盖 `main.js` 即可：

```bash
cp main.js <vault>/.obsidian/plugins/ink/main.js
```

## 给 AI 使用指引

### 代码结构

```
ink/
├── manifest.json      # 插件清单
├── main.js            # 全部逻辑（编译后，含 tldraw SDK、React、Obsidian 插件 API）
├── main.js.bak        # 官方原版（对比用）
├── styles.css         # 全部样式（.ddc_ink_ 前缀）
└── data.json          # 配置样例
```

### 关键搜索锚点

| 关键词 | 所在行 | 用途 |
|--------|--------|------|
| `WritingEmbedWidget` | ~75100 | 阅读视图嵌入块的渲染类（魔改位置） |
| `_renderTldraw()` | ~75120 | 魔改新增：渲染 tldraw 画布 |
| `_unloadTldraw()` | ~75130 | 魔改新增：卸载 tldraw 画布 |
| `buildWritingEmbed()` | ~17076 | 构建嵌入代码（含 `<!-- ink: -->` + JSON） |
| `buildDrawingEmbed()` | ~17090 | 构建绘画嵌入代码 |
| `insertNewWritingFile` | ~75383 | 创建新 handwriting section 的入口 |
| `implementWritingEmbedActions` | ~76591 | 注册三个 writing 命令 |
| `WRITE_EMBED_KEY` | ~17031 | `handwritten-ink`（代码块标识） |
| `.ddc_ink_resize-container` | styles.css | 嵌入块容器样式 |

### 如需其他修改

- **样式修改** → 编辑 `styles.css`，搜索 `.ddc_ink_` 前缀
- **修改嵌入内容**（如你说的"明文插入画布名"）→ 编辑 `buildWritingEmbed()` 函数
- **修改折叠行为** → 编辑 `WritingEmbedWidget.onload()` 中的折叠条逻辑
- **重新编译**：本项目无 TS 源码，若需从源码编译，请去 [daledesilva/obsidian_ink](https://github.com/daledesilva/obsidian_ink) 获取 TypeScript 源码，打上本 repo 的 diff 后重新编译

### 数据文件

笔迹数据存储在 `.writing` 文件中（JSON 格式，tldraw `TLDrawDocument`），默认在 `Ink/Writing/` 子目录下。笔记中通过 `<!-- ink: 路径/文件名.writing -->` 嵌入。

注：`.writing` 文件里的 `previewUri` 字段是 base64 缩略图（可能很大），全文搜索时建议跳过。

---

> 打包时间：2026-07-07
> 回退官方版：用 `main.js.bak` 覆盖 `main.js` 即可
