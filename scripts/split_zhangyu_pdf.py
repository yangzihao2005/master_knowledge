#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
split_zhangyu_pdf.py — 《张宇考研数学基础30讲》PDF 按讲切分

配合 Obsidian `PDF++` 使用：PDF 本体只做本地拷贝、不进 Git（`*.pdf` 已忽略），
Git 里只保留 `01.高数/张宇30讲.md` 里的框选坐标链接。

功能
----
1. 读取源 PDF 书签（TOC），筛选 `第N讲` / `第N章`，自动算出每一讲的 [起始页, 结束页]
2. 无书签时降级使用文件顶部的 `MANUAL_CHAPTERS`，并打印前 10 页文本辅助校准页码
3. 输出到 `01.高数/张宇30讲_PDF/`，命名 `01_第01讲_函数极限与连续.pdf`
4. 生成唯一入口 `01.高数/张宇30讲.md`（已存在且含手写内容时默认不覆盖）

用法
----
    python scripts/split_zhangyu_pdf.py                       # 自动扫描仓库内 >100MB 的 PDF
    python scripts/split_zhangyu_pdf.py "D:/path/book.pdf"     # 指定源文件
    python scripts/split_zhangyu_pdf.py --dry-run             # 只打印切分计划
    python scripts/split_zhangyu_pdf.py --force               # 覆盖已存在的 PDF 与 md
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖 PyMuPDF，请先执行：pip install pymupdf")

# ---------------------------------------------------------------------------
# 降级配置：当 PDF 没有可用书签时使用（页码 = PDF 物理页码，从 1 开始，闭区间）
# 本机《27张宇基础30讲（高数）》为纯扫描件、无书签，已按分讲标题页逐页核对。
# 印刷页码 + 6 = PDF 物理页码。
# 格式：(序号, 标签, 起始页, 结束页, 标题)
# ---------------------------------------------------------------------------
MANUAL_CHAPTERS: list[tuple[int, str, int, int, str]] = [
    (1, "第01讲", 7, 81, "函数极限与连续"),
    (2, "第02讲", 82, 104, "数列极限"),
    (3, "第03讲", 105, 124, "一元函数微分学的概念"),
    (4, "第04讲", 125, 144, "一元函数微分学的计算"),
    (5, "第05讲", 145, 169, "一元函数微分学的应用（一）——几何应用"),
    (6, "第06讲", 170, 191, "一元函数微分学的应用（二）——中值定理、微分等式与微分不等式"),
    (7, "第07讲", 192, 200, "一元函数微分学的应用（三）——物理应用与经济应用"),
    (8, "第08讲", 201, 235, "一元函数积分学的概念与性质"),
    (9, "第09讲", 236, 268, "一元函数积分学的计算"),
    (10, "第10讲", 269, 287, "一元函数积分学的应用（一）——几何应用"),
    (11, "第11讲", 288, 299, "一元函数积分学的应用（二）——积分等式与积分不等式"),
    (12, "第12讲", 300, 309, "一元函数积分学的应用（三）——物理应用与经济应用"),
    (13, "第13讲", 310, 343, "多元函数微分学"),
    (14, "第14讲", 344, 382, "二重积分"),
    (15, "第15讲", 383, 414, "微分方程"),
    (16, "第16讲", 415, 469, "无穷级数（仅数学一、数学三）"),
    (17, "第17讲", 470, 493, "多元函数积分学的预备知识（仅数学一）"),
    (18, "第18讲", 494, 551, "多元函数积分学（仅数学一）"),
    (19, "附录1", 552, 554, "图像变换"),
    (20, "附录2", 555, 557, "常用平面图形"),
    (21, "附录3", 558, 560, "常用空间图形"),
    (22, "附录4", 561, 563, "重要公式"),
    (23, "附录5", 564, 568, "从指数函数到双曲函数"),
    (24, "附录6", 569, 586, "变形技巧"),
]

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "01.高数" / "张宇30讲_PDF"
MD_PATH = REPO_ROOT / "01.高数" / "张宇30讲.md"
MD_HEADER = "# 📚 张宇30讲（PDF++ 摘录与错题总表）\n"
ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')
TOC_RE = re.compile(r"^\s*第\s*(\d+)\s*([讲章])\s*[：:、.．\s]*(.*)$")


def safe_name(text: str) -> str:
    """过滤 Windows 非法字符，压缩多余空白。"""
    return re.sub(r"\s+", " ", ILLEGAL.sub("", text)).strip().strip(".")


def find_source_pdf(cli_path: str | None) -> Path:
    """CLI 指定 > 当前目录下 >100MB 的 PDF > 仓库根目录向下扫描。"""
    if cli_path:
        p = Path(cli_path).expanduser()
        if not p.is_file():
            sys.exit(f"找不到源 PDF：{p}")
        return p

    candidates: list[Path] = []
    for base in (Path.cwd(), REPO_ROOT):
        for p in base.glob("*.pdf"):
            if p.is_file() and p.stat().st_size > 100 * 1024 * 1024:
                candidates.append(p)
    if candidates:
        candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
        print(f"自动选中源 PDF：{candidates[0]}（{candidates[0].stat().st_size / 1048576:.1f} MB）")
        return candidates[0]

    sys.exit(
        "未找到大于 100MB 的 PDF，请显式传入路径：\n"
        '  python scripts/split_zhangyu_pdf.py "C:/Users/你/Desktop/27张宇基础30讲（高数）.pdf"'
    )


def chapters_from_toc(doc: "fitz.Document") -> list[tuple[int, str, int, int, str]]:
    """优先从书签解析；解析不到返回空列表。"""
    try:
        toc = doc.get_toc(simple=True)
    except Exception:  # pragma: no cover
        toc = []
    if not toc:
        return []

    hits: list[tuple[int, str, int, int, str]] = []
    seen: set[str] = set()
    for _level, title, page in toc:
        m = TOC_RE.match(title or "")
        if not m or not page or page < 1:
            continue
        kind = m.group(2)
        label = f"第{int(m.group(1)):02d}{kind}" if kind == "讲" else f"第{int(m.group(1)):02d}章"
        if label in seen:
            continue
        seen.add(label)
        hits.append((int(m.group(1)), label, page, 0, safe_name(m.group(3)) or label))

    hits.sort(key=lambda x: x[2])
    total = doc.page_count
    fixed: list[tuple[int, str, int, int, str]] = []
    for i, (num, label, start, _, title) in enumerate(hits):
        end = hits[i + 1][2] - 1 if i + 1 < len(hits) else total
        fixed.append((i + 1, label, start, min(end, total), title))
    return fixed


def chapters_from_manual(doc: "fitz.Document") -> list[tuple[int, str, int, int, str]]:
    if not MANUAL_CHAPTERS:
        return []
    total = doc.page_count
    out = []
    for num, label, start, end, title in MANUAL_CHAPTERS:
        if start > total:
            print(f"⚠️ 跳过越界配置：{label} 起始页 {start} > 总页数 {total}")
            continue
        out.append((num, label, start, min(end, total), title))
    return out


def probe_first_pages(doc: "fitz.Document", pages: int = 10) -> None:
    """无书签且无手工配置时，打印前 N 页文本辅助定位目录页码。"""
    print(f"\n未找到书签，也未配置 MANUAL_CHAPTERS。先打印前 {pages} 页文本：\n" + "=" * 60)
    for i in range(min(pages, doc.page_count)):
        text = doc[i].get_text().strip()
        show = text[:1500]
        print(f"--- PDF 第 {i + 1} 页 ---")
        print(show if show else "（本页无文字层，可能是扫描页，请直接翻看或手工填页码）")
    print("=" * 60)
    print("请把每讲的 [起始页, 结束页] 填进脚本顶部的 MANUAL_CHAPTERS 后重新运行。\n")


def build_markdown(chapters: list[tuple[int, str, int, int, str]]) -> str:
    lines = [MD_HEADER.rstrip("\n"), ""]
    for _num, label, _start, _end, title in chapters:
        stem = f"{_num:02d}_{label}_{safe_name(title)}"
        lines.append(f"## {label}：[[{OUT_DIR.name}/{stem}.pdf]]")
        lines.append("")
        lines.append(f"> {label} {title}（共 {_end - _start + 1} 页）")
        lines.append("")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="按讲切分《张宇30讲》PDF 并生成 PDF++ 入口文档")
    ap.add_argument("pdf", nargs="?", help="源 PDF 路径（省略则自动扫描 >100MB 的 PDF）")
    ap.add_argument("--dry-run", action="store_true", help="只打印切分计划，不写文件")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的分册与 md")
    args = ap.parse_args()

    src = find_source_pdf(args.pdf)
    doc = fitz.open(src)
    total = doc.page_count
    print(f"源文件：{src}\n总页数：{total}")

    chapters = chapters_from_toc(doc)
    source = "PDF 内置书签"
    if not chapters:
        chapters = chapters_from_manual(doc)
        source = "MANUAL_CHAPTERS 手工配置"
    if not chapters:
        probe_first_pages(doc)
        return

    print(f"章节来源：{source}，共 {len(chapters)} 段\n")
    for num, label, start, end, title in chapters:
        print(f"  {num:02d}  {label}  p{start:>4}-{end:<4} ({end - start + 1:>3} 页)  {title}")

    if args.dry_run:
        print("\n[dry-run] 未写入任何文件。")
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for num, label, start, end, title in chapters:
        stem = f"{num:02d}_{label}_{safe_name(title)}"
        out_path = OUT_DIR / f"{stem}.pdf"
        if out_path.exists() and not args.force:
            print(f"跳过已存在：{out_path.name}")
            continue
        sub = fitz.open()
        sub.insert_pdf(doc, from_page=start - 1, to_page=end - 1)
        sub.save(str(out_path), garbage=4, deflate=True)
        sub.close()
        written += 1
        print(f"✅ {out_path.name}  ({out_path.stat().st_size / 1048576:.1f} MB)")

    if MD_PATH.exists() and not args.force:
        print(f"\n⚠️ {MD_PATH.name} 已存在，为避免覆盖你粘贴的 PDF++ 摘录，本次不改动。")
        print("   如需按当前分册重建目录，请加 --force。")
    else:
        MD_PATH.parent.mkdir(parents=True, exist_ok=True)
        MD_PATH.write_text(build_markdown(chapters), encoding="utf-8")
        print(f"\n📄 已生成入口文档：{MD_PATH}")

    print(f"\n完成：新切分 {written} 个分册 → {OUT_DIR}")


if __name__ == "__main__":
    main()
