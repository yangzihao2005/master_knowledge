#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_all_display_issues.py — 全库 Markdown 显示问题一站式修复

对仓库下所有 .md 笔记执行 4 步修复：
  1) 代码块围栏粘连修复 + 裸代码块自动补全语言（cpp / python / html），随后隔离保护
  2) LaTeX 分隔符统一：\\( \\) → $...$；\\[ \\] → $$ 块；行内 $ 剥离内侧空格；
     块级 $$ 规范为独立成行；行内公式末尾中文标点外移
  3) 表格上方补空行、标题 # 规范空格/上方空行、列表 "- 内容" 补空格
  4) 还原保护区（YAML / 代码块 / 行内代码 / wikilink / URL），压缩冗余空行

用法：
  python scripts/fix_all_display_issues.py            # 实际修复并写入
  python scripts/fix_all_display_issues.py --dry-run  # 只扫描出报告，不写入
"""

import argparse
import os
import re
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_DIRS = {'.git', '.obsidian', '.trash', 'node_modules', '.agents', '.claudian', 'scripts', '_system'}
SKIP_FILES = {'经济记录.md', 'B站收藏夹.md', '任务.md', 'AGENTS.md'}

stats = Counter()
samples = []          # (类别, 相对路径, before, after)
MAX_SAMPLES = 60

# ---------------------------------------------------------------- placeholders
_TOK_RE = re.compile('\x00MDFIX(\\d+)\x00')
_ph = {}

def _protect(body):
    tok = '\x00MDFIX{}\x00'.format(len(_ph))
    _ph[tok] = body
    return tok

def _restore(text):
    return _TOK_RE.sub(lambda m: _ph[m.group(0)], text)

def _dump_samples():
    for cat, path, before, after in samples[:MAX_SAMPLES]:
        print('[{}] {}'.format(cat, path))
        print('  - ' + repr(before))
        print('  + ' + repr(after))

# ---------------------------------------------------------------- fence fixes
FENCE_RE = re.compile(r'^\s*```')

CPP_MARKS = ('#include', 'int main', 'printf', 'scanf', 'malloc', 'struct ', 'typedef ',
             'ElemType', 'LNode', 'BiTree', 'vector<', 'cout', 'cin', 'std::')
PY_MARKS = ('def ', 'import ', 'print(')
HTML_MARKS = ('<html', '<div', '<!DOCTYPE')

def detect_lang(body):
    low = body.lower()
    if any(m.lower() in low for m in CPP_MARKS):
        return 'cpp'
    if any(m.lower() in low for m in PY_MARKS):
        return 'python'
    if any(m.lower() in low for m in HTML_MARKS):
        return 'html'
    return ''

def split_glued_fences(lines, path):
    """水文与 ``` 粘在一行的拆分化；返回新行列表。"""
    out = []
    inside = False
    for line in lines:
        stripped = line.strip()
        triplepos = line.find('```')
        if not inside and triplepos != -1 and line[:triplepos].strip():
            # 正文```lang 粘连：拆成正文一行 + 围栏一行
            stats['fence_glue_split'] += 1
            _sample('fence', path, line, line[:triplepos].rstrip() + ' || ' + line[triplepos:].rstrip())
            out.append(line[:triplepos].rstrip())
            tail = line[triplepos:]
            ml = re.match(r'^```+(\w*)', tail)
            tail = '```' + (ml.group(1) if ml else '')
            out.append(tail)
            inside = True
            continue
        if inside and not line.lstrip().startswith('```') and line.rstrip().endswith('```'):
            # code``` 粘连闭合：拆开
            e = line.rfind('```')
            if e > 0:
                stats['fence_glue_split'] += 1
                _sample('fence', path, line, line[:e].rstrip() + ' || ```')
                out.append(line[:e].rstrip())
                out.append('```')
                inside = False
                continue
        if FENCE_RE.match(line) and not stripped[3:].lstrip('`'):
            inside = not inside if not inside or stripped == '```' or re.match(r'^\s*```\s*$', line) else inside
        # 简化：维护开/闭状态
        if FENCE_RE.match(line):
            s = stripped
            if s[3:].strip() and not s[3:].startswith('`'):
                langish = s[3:].strip().split(' ')[0]
                if not inside and len(langish) > 0 and not re.match(r'^\w+$', langish):
                    pass  # 内容含 ``` 但不像语言，当作开栏（下方统一处理）
                inside = True
            else:
                inside = not inside
        out.append(line)
    return out

def fix_fences(lines, path):
    """保证围栏独立成行 + 前后空行 + 裸围栏补语言。"""
    lines = [l.rstrip() for l in lines]
    # 1) 定位围栏行（开/闭状态机）
    opens = []   # (idx, lang)
    closes = []
    inside = False
    for i, line in enumerate(lines):
        s = line.strip()
        if not s.startswith('```'):
            continue
        rest = s[3:].strip()
        if not inside:
            if rest and not re.match(r'^\w[\w+\-._]*$', rest):
                # 尾巴不像语言（如 "1}`）的内容行含 ```，跳过
                continue
            if rest == '' or re.match(r'^\w[\w+\-._]*$', rest):
                opens.append((i, rest))
                inside = True
        else:
            if re.match(r'^`*$', rest):
                closes.append(i)
                inside = False
    # 未闭合围栏：补一个闭合（尽量少做，但保证后续解析安全）
    if inside and opens:
        lines.append('```')
        closes.append(len(lines) - 1)
        stats['fence_unclosed_closed'] += 1
    # 2) 开闭行独立成行已天然满足；补前后空行
    fence_idx = {i for i, _ in opens} | set(closes)
    new_lines = []
    for i, line in enumerate(lines):
        if (i in fence_idx and new_lines and new_lines[-1].strip()
                and not _TOK_RE.search(new_lines[-1])):
            new_lines.append('')
            stats['fence_blank_added'] += 1
        new_lines.append(line)
        if i in fence_idx and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if nxt and not _TOK_RE.search(lines[i + 1]):
                new_lines.append('')
                stats['fence_blank_added'] += 1
    lines = new_lines
    # 3) 裸围栏补语言
    inside = False
    open_idx = None
    cur = []
    for i, line in enumerate(lines):
        s = line.strip()
        if not s.startswith('```'):
            continue
        rest = s[3:].strip()
        if not inside:
            inside = True
            open_idx = i
            cur = []
        else:
            inside = False
            if open_idx is not None:
                lang = lines[open_idx].strip()[3:].strip()
                if lang == '':
                    pad = len(lines[open_idx]) - len(lines[open_idx].lstrip())
                    detected = detect_lang('\n'.join(cur))
                    if detected:
                        blank = 1 if open_idx - 1 >= 0 else 0
                        comp = blank + open_idx
                        lines[open_idx] = ' ' * pad + '```' + detected
                        stats['lang_added'] += 1
                        _sample('lang', _rel(path), '```' + '  (' + _rel(path) + ')',
                                '"```{}"  +\\n{} 行代码'.format(detected, comp))
                open_idx = None
                cur = []
    if inside is False:
        pass
    else:
        open_idx = None
    return lines

# ---------------------------------------------------------------- protection
FENCE_BLOCK_RE = re.compile(r'^[ \t]*```[^\n]*\n[\s\S]*?^[ \t]*```[ \t]*$', re.M)
INLINE_CODE_RE = re.compile(r'`{1,3}[^`\n]+`{1,3}')
WIKILINK_RE = re.compile(r'!?\[\[[^\[\]\n]+\]\]')
MD_LINK_RE = re.compile(r'\[[^\[\]\n]*\]\([^)\n]+\)')
URL_RE = re.compile(r'(?<![\\\w`])(https?://[^\s)\]\n]+)')
FRONTMATTER_RE = re.compile(r'\A---\n[\s\S]*?\n---(?:\n|$)')

def protect_markup(text):
    # YAML frontmatter
    m = FRONTMATTER_RE.match(text)
    if m:
        text = _protect(m.group(0)) + text[m.end():]
    # 围栏代码块（多行整块隔离）
    text = FENCE_BLOCK_RE.sub(lambda m: _protect(m.group(0)), text)
    # 行内代码
    text = INLINE_CODE_RE.sub(lambda m: _protect(m.group(0)), text)
    # wikilink / PDF++ 坐标链接 / 嵌入
    text = WIKILINK_RE.sub(lambda m: _protect(m.group(0)), text)
    # markdown 链接 + 裸 URL
    text = MD_LINK_RE.sub(lambda m: _protect(m.group(0)), text)
    text = URL_RE.sub(lambda m: _protect(m.group(0)), text)
    return text

# ---------------------------------------------------------------- math fixes
CJK_PUNCT = r'[\u3001\u3002\uff0c\uff1b\uff1a\uff01\uff1f]'
ESC_DOLLAR_BLOCK_RE = re.compile(r'\\\[([\s\S]+?)\\\]')
DOLLAR2_TMP = '\x01D2\x01'

def is_mathy(content):
    if not content or not content.strip():
        return False
    if re.search(r'[\u4e00-\u9fff]|[\u3000-\u303f]|[\uff01-\uff5e]', content):
        return False
    return bool(re.search(r'[A-Za-z\\^_{}=+\-*/<>]', content))

def fix_inline_dollars(text, path):
    lines = text.split('\n')
    out = []
    for line in lines:
        if '$' not in line or '\x00' in line:
            out.append(line)
            continue
        # 转换块级 $$ 占位符，避免误配
        tmp = line.replace('$$', DOLLAR2_TMP)
        if DOLLAR2_TMP in tmp:
            # 行里含 $$：块级公式行，跳过行内处理
            out.append(line)
            continue
        # 找成对 $（跳过 \$ 转义）
        positions = []
        i = 0
        while i < len(tmp):
            c = tmp[i]
            if c == '\\':
                i += 2
                continue
            if c == '$':
                positions.append(i)
            i += 1
        if len(positions) < 2 or len(positions) % 2 != 0:
            out.append(line)
            continue
        # 从后向前替换
        pairs = [(positions[k], positions[k+1]) for k in range(0, len(positions), 2)]
        new = tmp
        for (a, b) in reversed(pairs):
            content = new[a+1:b]
            moved = ''
            while content.endswith(('', '，', '。', '；', '、', '！', '？')) and \
                    re.match(r'.*[\u3001\u3002\uff0c\uff1b\uff01\uff1f]$', content):
                moved = content[-1] + moved
                content = content[:-1]
            valid = is_mathy(content)
            inner = content.strip()
            if not valid or inner == content and not moved:
                continue
            if inner != content or moved or re.match(r'^\s', content) or re.search(r'\s$', content):
                stats['inline_trim'] += 1
                old = new[a:b+1]
                replay = new[:a] + '$' + inner + '$' + moved + new[b+1:]
                _sample('inline$', path, old, replay[a:b+1+len(moved)::1] if False else new[a:b+1])
                _sample('inline$2', path, new[a:b+1], '$' + inner + '$' + moved)
                new = replay
        out.append(new)
    return '\n'.join(out)

def normalize_block_dollars(text, path):
    """把跨行 / 含 \\begin 的 $$...$$ 规范为独立成行并与正文间留空行。"""
    positions = []
    i = 0
    while i < len(text):
        if text.startswith('$$', i):
            positions.append(i)
            i += 2
            continue
        if text[i] == '\\':
            i += 2
            continue
        i += 1
    if len(positions) == 0 or len(positions) % 2 != 0:
        if len(positions) % 2 != 0:
            print('  警告: {} 有未配对的 $$，跳过块级规范化'.format(path))
        return text
    for k in range(0, len(positions), 2):
        p1, p2 = positions[k], positions[k+1]
        content = text[p1+2:p2]
        need = '\n' in content or '\\begin{' in content
        if not need:
            continue
        ops = []
        # 开栏所在行
        s_idx = text.rfind('\n', 0, p1) + 1
        prefix = text[s_idx:p1]
        if prefix.strip():
            ops.append((p1, '\n'))                       # 正文与 $$ 分行
            ops.append((p1, '\n'))  # 空行（两个都插在 p1 前会合并为两个 \n）
            del ops[-1]
            ops = ops[:1]
            ops.append((p1, '\n'))
        else:
            # $$ 在行首：检查上一行是否为空
            if s_idx > 0 and text[s_idx-1] == '\n':
                prev = text[:s_idx-1].rsplit('\n', 1)[-1]
                if prev.strip() and not prev.strip().startswith('```'):
                    ops.append((s_idx, '\n'))
        # 闭栏所在行
        e_pos = text.find('\n', p2)
        suffix = text[p2+2:e_pos if e_pos != -1 else len(text)]
        if text[p2-1] != '\n' and p2 > p1:
            ops.append((p2, '\n'))                       # 内容与闭合 $$ 分行
        if suffix.strip():
            if text[p2+2] != '\n' if p2 + 2 < len(text) else False:
                ops.append((p2+2, '\n'))                  # $$ 与后文分行
            ops.append((p2+2, '\n'))                      # $$ 与后文间空行
        else:
            # $$ 在行尾：下一行非空则补空行
            if e_pos != -1:
                nxt = text[e_pos+1:].split('\n', 1)[0]
                if nxt.strip():
                    ops.append((e_pos+1, '\n'))
        if not ops:
            continue
        # 同位置去重
        ops = sorted(set(ops), key=lambda x: -x[0])
        for pos, ins in ops:
            text = text[:pos] + ins + text[pos:]
        stats['block_dollar_norm'] += 1
        _sample('$$', path,
                (prefix + '$$' + content.strip()[:50] if prefix.strip() else '$$' + content.strip()[:50]),
                '$$\n' + content.strip()[:50] + '\n$$ (独立成行)')
    return text

def fix_math(text, path):
    m = FRONTMATTER_RE.match(text)
    pre = ''
    if m:
        pre = m.group(0)
        text = text[m.end():]
    text = ESC_DOLLAR_BLOCK_RE.sub(lambda m: '\n$$\n' + m.group(1).strip().strip('\n') + '\n$$\n\n', text)
    text = INLINE_PAREN_RE.sub(lambda m: '$' + m.group(1).strip() + '$', text) if False else text
    text = re.sub(r'\\\[', '$$', text)   # 兜底: 单边 \[ (少见)
    text = re.sub(r'\\\]', '$$', text)   # 兜底: 单边 \]
    text = INLINE_PAREN_RE.sub(lambda m: '$' + m.group(1).strip() + '$', text)
    stats['paren_to_dollar'] = stats.get('paren_to_dollar', 0)
    text = normalize_block_dollars(text, path)
    text = fix_inline_dollars(text, path)
    return pre + text

# ---------------------------------------------------------------- structure
HEADING_FIX_RE = re.compile(r'^(\s*)(#{1,6})([^\s#].*)$', re.M)

def fix_headings_tables_lists(text, path):
    lines = text.split('\n')
    # 计算每行是否在 $$ 块内
    in_math = [False] * len(lines)
    inside = False
    for i, line in enumerate(lines):
        if re.match(r'^\s*\$\$\s*$', line):
            inside = not inside
            in_math[i] = True
            continue
        in_math[i] = inside

    HEADING_START_RE = re.compile(r'^\s*#{1,6}\s')
    new = []
    for i, line in enumerate(lines):
        if '\x00' in line:
            new.append(line)
            continue
        # —— 标题 # 后空格 ——
        m = re.match(r'^(\s*)(#{1,6})([^\s#].*)$', line)
        if m and not in_math[i]:
            n = len(m.group(2))
            rest = m.group(3)
            fixable = n >= 2 or re.match(r'^\d', rest)
            if fixable:
                old = line
                line = m.group(1) + m.group(2) + ' ' + rest
                stats['heading_space'] += 1
                _sample('heading', path, old, line)
        # —— 行中标题粘连拆分 ——
        msplit = re.match(r'^(.+[^\s])\s(#{2,6}\s+.*)$', line)
        if msplit and not in_math[i]:
            old = line
            head_part = msplit.group(1).rstrip()
            heading_part = msplit.group(2)
            # 仅当第二部分长得像标题（行尾以非句号结尾）
            if len(heading_part) < 60:
                new.append(head_part)
                new.append(heading_part)
                stats['heading_inline_split'] += 1
                _sample('heading', path, old, head_part + ' || ' + heading_part)
                continue
        # —— 列表 `-内容` 补空格 ——
        mlist = re.match(r'^(\s*)-(?![\s\-\d])(.+)$', line)
        if mlist and not in_math[i]:
            old = line
            line = mlist.group(1) + '- ' + mlist.group(2)
            stats['list_space'] += 1
            _sample('list', path, old, line)
        # —— 标题上方空行 ——
        if HEADING_START_RE.match(line) and not in_math[i] and new:
            prev = new[-1]
            if prev.strip() and not _TOK_RE.search(prev) and not HEADING_START_RE.match(prev):
                if not re.match(r'^\s*\$+\s*$', prev):
                    new.append('')
                    stats['heading_blank'] += 1
        new.append(line)
    lines = new

    # —— 表格上方空行 ——
    is_table_row = lambda s: (not '\x00' in s and s.lstrip().startswith('|') and s.count('|') >= 2)
    i = 0
    out = []
    while i < len(lines):
        if (not out and i < len(lines) and is_table_row(lines[i])
                and i + 1 < len(lines) and is_table_row(lines[i+1])):
            j = i
            while j < len(lines) and is_table_row(lines[j]):
                j += 1
            if out and out[-1].strip():
                out.append('')
                stats['table_blank'] += 1
            out.extend(lines[i:j])
            i = j
            continue
        if (is_table_row(lines[i]) and i + 1 < len(lines) and is_table_row(lines[i+1])):
            if out and out[-1].strip():
                out.append('')
                stats['table_blank'] += 1
        out.append(lines[i])
        i += 1
    return '\n'.join(out)

# ---------------------------------------------------------------- misc
BLANK_RUN_RE = re.compile(r'\n{4,}')

def collapse_blanks(text):
    def repl(m):
        n = m.group(0).count('\n')
        if n - 1 >= 3:
            stats['blank_collapsed'] += n - 1 - 2
        return '\n\n\n'
    return BLANK_RUN_RE.sub(repl, text)

def collapse_blanks(text):
    def repl(m):
        nl = m.group(0).count('\n')
        blanks = nl - 1
        if blanks >= 3:
            stats['blank_collapsed'] += blanks - 2
        return '\n\n\n'
    return BLANK_RUN_RE.sub(repl, text)

def collapse_blanks2(text):
    return collapse_blanks(text)

# ---------------------------------------------------------------- pipeline
def process_text(text, relpath):
    """四步流水线。"""
    text = text.replace('\r\n', '\n')
    # 1) 围栏修复（先于保护）
    lines = text.split('\n')
    lines = fix_fences(lines, relpath)
    text = '\n'.join(lines)
    # 保护隔离
    text = protect_markup(text)
    # 2) 数学
    text = fix_math(text, relpath)
    # 3) 结构
    text = fix_headings_tables_lists(text, relpath)
    # 4) 还原
    text = _restore(text)
    text = collapse_blanks(text)
    return text

def _rel(path):
    return os.path.relpath(path, ROOT)

def _sample(cat, path, before, after):
    if len(samples) < MAX_SAMPLES:
        samples.append((cat, path, str(before)[:200], str(after)[:200]))

INLINE_PAREN_RE = re.compile(r'\\\((.+?)\\\)', re.S)

def find_md_files():
    found = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn in SKIP_FILES or not fn.endswith('.md'):
                continue
            found.append(os.path.join(dirpath, fn))
    return found

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    files = find_md_files()
    scanned = changed = 0
    for path in files:
        relpath = _rel(path)
        with open(path, 'rb') as f:
            raw = f.read()
        crlf = '\r\n' in raw
        enc = 'utf-8-sig' if raw[:3] == '\\xef\\xbb\\xbf'.encode().decode('latin-1', 'ignore')[:0] + 'utf-8-sig'[:0] or raw.startswith(b'\xef\xbb\xbf') else 'utf-8'
        try:
            original = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        scanned += 1
        try:
            fixed = process_text(original, relpath)
        except Exception as exc:
            print('异常跳过 {}: {}'.format(relpath, exc))
            continue
        if crlf:
            fixed = fixed.replace('\n', '\r\n')
        if fixed != original:
            changed += 1
            if not args.dry_run:
                with open(path, 'w', encoding=enc, newline='') as f:
                    f.write(fixed)

    print('=' * 60)
    print('全库显示问题扫描报告{}'.format('（dry-run，未写入）' if args.dry_run else ''))
    print('=' * 60)
    print('扫描文件数: {}'.format(scanned))
    print('修改文件数: {}'.format(changed))
    print('-' * 60)
    print('公式修复总数: {}  (\\(\\)→$: {}, \\[\\]块规范化: {}, 行内$剥空格: {})'.format(
        stats.get('paren_plus', 0) + stats.get('inline_trim', 0),
        stats.get('paren_to_dollar', 0), stats.get('block_dollar_norm', 0), stats.get('inline_trim', 0)))
    print('代码块修复数: {}  (粘连拆分: {}, 裸围栏补语言: {}, 未闭合补全: {}, 围栏前后空行: {})'.format(
        stats.get('fence_glue_split', 0) + stats.get('lang_added', 0) + stats.get('fence_unclosed_closed', 0),
        stats.get('fence_glue_split', 0), stats.get('lang_added', 0),
        stats.get('fence_unclosed_closed', 0), stats.get('fence_blank_added', 0)))
    print('表格标题列表修复数: {}  (表格空行: {}, 标题空格: {}, 标题空行: {}, 行中标题拆分: {}, 列表空格: {})'.format(
        stats.get('table_blank', 0) + stats.get('heading_space', 0) + stats.get('heading_blank', 0)
        + stats.get('heading_inline_split', 0) + stats.get('list_space', 0),
        stats.get('table_blank', 0), stats.get('heading_space', 0),
        stats.get('heading_blank', 0), stats.get('heading_inline_split', 0), stats.get('list_space', 0)))
    print('冗余空行压缩: {} 处'.format(stats.get('blank_collapsed', 0)))
    print('=' * 60)
    print()
    print('典型修改示例（前 {} 处）:'.format(min(10, len(samples))))
    _dump_samples()

if __name__ == '__main__':
    main()
