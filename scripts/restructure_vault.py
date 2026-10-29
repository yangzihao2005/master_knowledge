#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
restructure_vault.py —— Obsidian 知识库根目录极简重构

把散落在根目录的分类与系统杂物，合并成「3 大业务分类 + 1 个系统收纳仓」：

    1.考研数学/      ← 01.高数 / 02.线代 / 03.数二题库（含其内部的 张宇30讲_PDF）
    2.考研408/       ← 04.数据结构 / 05.计算机组成原理 / 06.操作系统 / 07.计算机网络
    3.复盘与题库/    ← 00.全局与方法 / 08.错题与重测库 / 09.答案库
    _system/         ← 10.转写结果 / media-lib / Ink / 仓库更新提示词 / README.md

根目录只保留：任务.md、B站收藏夹.md、经济记录.md、AGENTS.md、scripts/、链接.base。

移动完成后自动修复：
  1. 全库 .md/.base 里的「带路径双链」与 <!-- mistake: {"src": ...} --> 源路径；
  2. 正文 Ink 手稿引用（<!-- ink: ... -->、handwritten-ink 的 filepath）；
  3. scripts/ 下脚本里写死的目录常量；
  4. Ink / Media Extended 插件的附件目录设置；
  5. 最后打印极简目录树 + 校验带路径双链是否全部可解析。

用法：
    python scripts/restructure_vault.py --dry-run   # 只看计划，不动文件
    python scripts/restructure_vault.py             # 执行

说明：脚本幂等（目标已存在时跳过该条移动），可安全重跑。
"""

from __future__ import annotations

import argparse
import codecs
import json
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# =====================================================================
# 一、移动计划：(旧路径, 新父目录)
# =====================================================================

MOVES: list[tuple[str, str]] = [
    # 第 1 大类：考研数学
    ("01.高数", "1.考研数学"),
    ("02.线代", "1.考研数学"),
    ("03.数二题库", "1.考研数学"),
    # 第 2 大类：考研 408
    ("04.数据结构", "2.考研408"),
    ("05.计算机组成原理", "2.考研408"),
    ("06.操作系统", "2.考研408"),
    ("07.计算机网络", "2.考研408"),
    # 第 3 大类：复盘与题库
    ("00.全局与方法", "3.复盘与题库"),
    ("08.错题与重测库", "3.复盘与题库"),
    ("09.答案库", "3.复盘与题库"),
    # 系统收纳仓
    ("10.转写结果", "_system"),
    ("media-lib", "_system"),
    ("Ink", "_system"),
    ("仓库更新提示词", "_system"),
    ("README.md", "_system"),
]

# 旧一级目录 → 新路径前缀（用于双链/文本路径修复）
PREFIX_MAP: dict[str, str] = {
    old: f"{parent}/{old}" for old, parent in MOVES if not old.endswith(".md")
}

# 新一级目录，用于避免重复加前缀（幂等）
NEW_PARENTS = ("1.考研数学", "2.考研408", "3.复盘与题库", "_system")

# 需要做文本路径修复的后缀
REWRITE_SUFFIXES = {".md", ".base", ".js", ".py"}

# 不参与文本修复的目录（原始 ASR 转写、历史提示词、缓存、插件源码）
SKIP_DIRS = {
    ".git",
    ".trash",
    ".obsidian",
    ".claudian",
    "node_modules",
    "__pycache__",
    "_system/10.转写结果",
    "_system/仓库更新提示词",
    "_system/README.md",  # 目录树等章节在移动后手工重写，避免机械替换打乱排版
}

# =====================================================================
# 二、工具函数
# =====================================================================


def read_text(path: Path) -> tuple[str, bool]:
    """按 UTF-8 读取，返回 (文本, 是否带 BOM)，保留原始编码形态。"""
    raw = path.read_bytes()
    has_bom = raw.startswith(codecs.BOM_UTF8)
    if has_bom:
        raw = raw[len(codecs.BOM_UTF8) :]
    return raw.decode("utf-8", errors="surrogateescape"), has_bom


def write_text(path: Path, text: str, has_bom: bool) -> None:
    data = text.encode("utf-8", errors="surrogateescape")
    path.write_bytes((codecs.BOM_UTF8 + data) if has_bom else data)


def is_skipped(rel: Path) -> bool:
    parts = rel.parts
    for skip in SKIP_DIRS:
        skip_parts = tuple(Path(skip).parts)
        if parts[: len(skip_parts)] == skip_parts:
            return True
        # 任意层级出现同名目录（如嵌套的 __pycache__）
        if len(skip_parts) == 1 and skip in parts[:-1]:
            return True
    return False


def do_moves(dry_run: bool) -> list[tuple[str, str]]:
    """执行目录/文件移动，返回实际生效的 (旧相对路径, 新相对路径)。"""
    done: list[tuple[str, str]] = []
    for old, parent in MOVES:
        src = REPO / old
        dst_parent = REPO / parent
        dst = dst_parent / Path(old).name

        if not src.exists():
            print(f"  [跳过] {old:<22} 不存在（可能已重构过）")
            continue
        if dst.exists():
            print(f"  [跳过] {old:<22} 目标已存在：{dst.relative_to(REPO)}")
            continue

        print(f"  [移动] {old:<22} → {dst.relative_to(REPO)}")
        if not dry_run:
            dst_parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        done.append((old, dst.relative_to(REPO).as_posix()))
    return done


# 匹配「旧一级目录 + 斜杠」的路径段，例如 03.数二题库/ 、Ink/
_PREFIX_RE = re.compile(
    "(" + "|".join(re.escape(k) for k in sorted(PREFIX_MAP, key=len, reverse=True)) + ")(?=/)"
)


def rewrite_paths(text: str) -> tuple[str, int]:
    """把文本中的旧路径前缀替换为新前缀，返回 (新文本, 替换次数)。"""
    count = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal count
        head = text[: match.start()]
        # 已经带了新一级前缀（幂等保护，如 1.考研数学/01.高数/）
        if any(head.endswith(p + "/") for p in NEW_PARENTS):
            return match.group(0)
        count += 1
        return PREFIX_MAP[match.group(1)]

    return _PREFIX_RE.sub(repl, text), count


def rewrite_all_texts(dry_run: bool) -> tuple[int, int]:
    """遍历仓库文本文件修复路径，返回 (改动文件数, 替换总次数)。"""
    changed_files = 0
    total_hits = 0
    for path in REPO.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in REWRITE_SUFFIXES:
            continue
        rel = path.relative_to(REPO)
        if is_skipped(rel) or rel.as_posix() == "scripts/restructure_vault.py":
            continue
        try:
            text, has_bom = read_text(path)
        except UnicodeDecodeError:
            continue
        new_text, hits = rewrite_paths(text)
        if hits:
            changed_files += 1
            total_hits += hits
            if not dry_run:
                write_text(path, new_text, has_bom)
    return changed_files, total_hits


# =====================================================================
# 三、定向修复（插件设置 / 脚本常量 / 收藏夹文件名）
# =====================================================================

PLUGIN_PATCHES = [
    (
        ".obsidian/plugins/ink/data.json",
        [('"writingSubfolder": "Ink/Writing"', '"writingSubfolder": "_system/Ink/Writing"'),
         ('"drawingSubfolder": "Ink/Drawing"', '"drawingSubfolder": "_system/Ink/Drawing"')],
    ),
    (
        ".obsidian/plugins/media-extended/data.json",
        [('"playback.screenshot.folder-path": "/media-lib/picture"',
          '"playback.screenshot.folder-path": "/_system/media-lib/picture"')],
    ),
]

SCRIPT_PATCHES = [
    (
        "scripts/run_asr.js",
        [
            ('const OUTPUT_DIR = "D:\\\\master_knowledge\\\\10.转写结果";',
             'const OUTPUT_DIR = "D:\\\\master_knowledge\\\\_system\\\\10.转写结果";'),
            ("转写结果统一输出到仓库 10.转写结果/。", "转写结果统一输出到仓库 _system/10.转写结果/。"),
        ],
    ),
    (
        "scripts/split_zhangyu_pdf.py",
        [
            ('OUT_DIR = REPO_ROOT / "01.高数" / "张宇30讲_PDF"',
             'OUT_DIR = REPO_ROOT / "1.考研数学" / "01.高数" / "张宇30讲_PDF"'),
            ('MD_PATH = REPO_ROOT / "01.高数" / "张宇30讲.md"',
             'MD_PATH = REPO_ROOT / "1.考研数学" / "01.高数" / "张宇30讲.md"'),
            (r"输出到 `01.高数/张宇30讲_PDF/`", r"输出到 `1.考研数学/01.高数/张宇30讲_PDF/`"),
            (r"生成唯一入口 `01.高数/张宇30讲.md`", r"生成唯一入口 `1.考研数学/01.高数/张宇30讲.md`"),
            (r"Git 里只保留 `01.高数/张宇30讲.md`", r"Git 里只保留 `1.考研数学/01.高数/张宇30讲.md`"),
        ],
    ),
]


def apply_patches(dry_run: bool) -> int:
    """应用定向字符串替换，返回改动文件数。"""
    changed = 0
    for rel, pairs in PLUGIN_PATCHES + SCRIPT_PATCHES:
        path = REPO / rel
        if not path.exists():
            print(f"  [跳过] 未找到 {rel}")
            continue
        text, has_bom = read_text(path)
        new_text = text
        for old, new in pairs:
            if old in new_text:
                new_text = new_text.replace(old, new)
        if new_text != text:
            changed += 1
            print(f"  [修补] {rel}")
            if not dry_run:
                write_text(path, new_text, has_bom)
        else:
            print(f"  [跳过] {rel} 无需修改")
    return changed


def patch_bili_sync() -> None:
    """让 B 站同步脚本认准根目录的 B站收藏夹.md（兼容旧名 收藏夹.md）。"""
    path = REPO / "scripts" / "bili_sync.py"
    if not path.exists():
        return
    text, has_bom = read_text(path)
    old_const = 'FAV_FILE_NAME = "收藏夹.md"'
    new_const = (
        '# 根目录唯一的收藏夹文件：优先 B站收藏夹.md，兼容旧名 收藏夹.md\n'
        'FAV_FILE_NAMES = ("B站收藏夹.md", "收藏夹.md")'
    )
    if new_const in text:
        return
    if old_const not in text:
        print("  [跳过] bili_sync.py 常量形态已变，请手动确认")
        return
    text = text.replace(old_const, new_const)

    old_prop = """    @property
    def fav_file(self) -> Path:
        return self.vault / FAV_FILE_NAME
"""
    new_prop = """    @property
    def fav_file(self) -> Path:
        for name in FAV_FILE_NAMES:
            candidate = self.vault / name
            if candidate.exists():
                return candidate
        return self.vault / FAV_FILE_NAMES[0]
"""
    if old_prop not in text:
        print("  [跳过] bili_sync.py fav_file 形态已变，请手动确认")
        return
    text = text.replace(old_prop, new_prop)

    # 日志里引用文件名的地方改用实际路径名
    text = text.replace(
        'log.info("本地 %s 共有 %d 个视频条目。", FAV_FILE_NAME, len(videos))',
        'log.info("本地 %s 共有 %d 个视频条目。", cfg.fav_file.name, len(videos))',
    )
    text = text.replace(
        'log.info("检测到旧版收藏库（%d 篇笔记），迁移进 %s ……", len(paths), FAV_FILE_NAME)',
        'log.info("检测到旧版收藏库（%d 篇笔记），迁移进 %s ……", len(paths), cfg.fav_file.name)',
    )
    write_text(path, text, has_bom)
    print("  [修补] scripts/bili_sync.py（收藏夹文件名解析）")


# =====================================================================
# 四、校验与展示
# =====================================================================

WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def check_wikilinks() -> list[tuple[str, str]]:
    """校验所有带路径的 wiki 双链能否解析，返回失效列表。"""
    broken: list[tuple[str, str]] = []
    for path in REPO.rglob("*.md"):
        rel = path.relative_to(REPO)
        if is_skipped(rel):
            continue
        try:
            text, _ = read_text(path)
        except UnicodeDecodeError:
            continue
        for m in WIKILINK_RE.finditer(text):
            target = m.group(1).split("|", 1)[0].split("#", 1)[0].strip()
            if "/" not in target and "\\" not in target:
                continue
            target = target.replace("\\", "/")
            candidates = [
                path.parent / target,
                REPO / target,
                path.parent / f"{target}.md",
                REPO / f"{target}.md",
            ]
            if not any(c.exists() for c in candidates):
                broken.append((rel.as_posix(), target))
    return broken


def print_tree(max_depth: int = 2) -> None:
    """打印重构后的极简目录树。"""
    hidden = {".git", ".obsidian", ".trash", ".claudian", "__pycache__"}

    def walk(directory: Path, prefix: str, depth: int) -> None:
        try:
            entries = sorted(
                (e for e in directory.iterdir() if e.name not in hidden),
                key=lambda e: (e.is_file(), e.name),
            )
        except PermissionError:
            return
        for idx, entry in enumerate(entries):
            last = idx == len(entries) - 1
            branch = "└── " if last else "├── "
            suffix = "/" if entry.is_dir() else ""
            print(f"{prefix}{branch}{entry.name}{suffix}")
            if entry.is_dir() and depth < max_depth:
                walk(entry, prefix + ("    " if last else "│   "), depth + 1)

    print(f"{REPO.name}/")
    walk(REPO, "", 1)


# =====================================================================
# 五、主流程
# =====================================================================


def main() -> int:
    parser = argparse.ArgumentParser(description="Obsidian 知识库根目录极简重构")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不修改文件")
    parser.add_argument("--depth", type=int, default=2, help="目录树打印深度（默认 2）")
    args = parser.parse_args()

    if not (REPO / "scripts").is_dir():
        print(f"✗ 未能在 {REPO} 找到 scripts/，请把脚本放回仓库的 scripts/ 目录再运行")
        return 1

    mode = "【演练模式】" if args.dry_run else "【执行模式】"
    print(f"{mode} 仓库根目录：{REPO}\n")
    print("── 1. 移动目录 ──")
    moved = do_moves(args.dry_run)

    print("\n── 2. 修复全库路径引用 ──")
    files, hits = rewrite_all_texts(args.dry_run)
    print(f"  文本路径：{files} 个文件 / {hits} 处替换")

    print("\n── 3. 修补插件与脚本常量 ──")
    apply_patches(args.dry_run)
    if args.dry_run:
        print("  [演练] 跳过 bili_sync.py 修补（需在真实移动后确认）")
    else:
        patch_bili_sync()

    if args.dry_run:
        print("\n（演练结束，未修改任何文件。去掉 --dry-run 正式执行。）")
        return 0

    manifest = REPO / "scripts" / "restructure_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "moved": [{"from": old, "to": new} for old, new in moved],
                "prefix_map": PREFIX_MAP,
                "rewritten_files": files,
                "rewritten_hits": hits,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print("\n── 4. 重构后的极简目录树 ──")
    print_tree(args.depth)

    print("\n── 5. 带路径双链校验 ──")
    broken = check_wikilinks()
    if not broken:
        print("  ✓ 所有带路径的双链均可解析")
    else:
        print(f"  ⚠ {len(broken)} 条双链未解析（可能是外部/待创建笔记）：")
        for source, target in broken[:30]:
            print(f"    - {source}  →  [[{target}]]")

    print(f"\n✓ 重构完成，移动 {len(moved)} 项，修复 {files} 个文件。")
    print("  回滚：git status 查看重命名记录，git restore/git clean 可还原。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
