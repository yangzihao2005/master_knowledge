#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_all_display_issues.py — 全库 Markdown 显示问题一站式修复

对仓库下所有 .md 笔记执行修复：
  1) 代码块围栏粘连修复 + 裸代码块自动补全语言（cpp / python / html），随后整块隔离保护
  2) LaTeX 分隔符统一：\\( \\) → $...$；\\[ \\] → $$ 块并独立成行；
     多行/含 \\begin 的 $$...$$ 强制规范为独立成行并与正文留空行；
     行内 $ 剥离内侧空格；行内公式末尾误包的中文标点外移
  3) 表格上方补空行；标题 # 后补空格 / 上方补空行；行中标题粘连拆分；列表 "- 内容" 补空格
  4) 还原保护区（YAML / 代码块 / 行内代码 / 双链嵌入 / PDF++ 链接 / URL），压缩冗余空行

用法：
  python scripts/fix_all_display_issues.py            # 实际修复并写入
  python scripts/fix_all_display_issues.py --dry-run  # 只出扫描报告，不写入
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
samples = []
MAX_SAMPLES = 60

def _sample(cat, path, before, after):
    if len(samples) < MAX_SAMPLES:
        samples.append((cat, path, str(before).replace('\n', '⏎')[:180],
                        str(after).replace('\n', '⏎')[:180]))

# ---------------------------------------------------------------- placeholders
_TOK_RE = re.compile('\x00MDFIX(\\d+)\x00')
_ph = {}

def _protect(body):
    tok = '\x00MDFIX{}\x00'.format(len(_ph))
    _ph[tok] = body
    return tok

def _restore(text):
    return _TOK_RE.sub(lambda m: _ph[m.group(0)], text)

# ---------------------------------------------------------------- fence fixes
LANGISH_RE = re.compile(r'^[\w+\-._]*\s*$')
CLOSE_RE = re.compile(r'^`{3,}\s*$')

def _fence_parts(line):
    s = line.lstrip()
    if not s.startswith('```'):
        return None
    m = re.match(r'^`{3,}(.*)$', s)
    if not m:
        return None
    return m.group(1).strip()          # '' 或语言名或多余内容

def fix_fences(lines, path):
    """返回 (新行组, open_langs{idx:lang}, close_idx_set, unclosed_bool)"""
    lines = list(lines)
    out = []
    inside = False
    kinds = {}          # idx -> 'open'/'close'
    for line in lines:
        trip = line.find('```')
        parts = _fence_parts(line)
        is_clean_fence = parts is not None and (parts == '' or re.match(r'^[\w+\-._]+$', parts))
        if not inside:
            if trip != -1 and line[:trip].strip():
                # 正文```lang 粘连 → 拆成两行
                m2 = re.match(r'^`{3,}([\w+\-._]*)', line[trip:])
                fence_line = '```' + (m2.group(1) if m2 else '')
                stats['fence_glue_split'] += 1
                _sample('fence', path, line, line[:trip].rstrip() + ' ⏎ ' + fence_line)
                out.append(line[:trip].rstrip())
                out.append(fence_line)
                kinds[len(out) - 1] = 'open'
                inside = True
                continue
            if trip == -1 and parts is not None:
                pass
            if is_clean_fence:
                out.append(line)
                kinds[len(out) - 1] = 'open'
                inside = True
                continue
            out.append(line)
            continue
        # inside
        if trip != -1 and not line.lstrip().startswith('```') \
                and line.rstrip().endswith('```') and len(line) - 3 >= 0:
            # code``` 粘连闭合 → 拆开（排除整行就是 ```）
            e = line.rfind('```')
            if line[:e].strip():
                stats['fence_glue_split'] += 1
                _sample('fence', path, line, line[:e].rstrip() + ' ⏎ ```')
                out.append(line[:e].rstrip())
                out.append('```')
                kinds[len(out) - 1] = 'close'
                inside = False
                continue
        if parts is not None and re.match(r'^`{3,}\s*$', line.lstrip()):
            out.append(line)
            kinds[len(out) - 1] = 'close'
            inside = False
            continue
        out.append(line)
    # 2) 围栏前后补空行（open 前面、close 后面），跳过占位符行
    new = []
    fence_idx = set(kinds)
    for i, line in enumerate(out):
        if i in fence_idx and i + 1 < len(out) and kinds[i] == 'open':
            if new and new[-1].strip() and '\x00' not in new[-1]:
                new.append('')
                stats['fence_blank_added'] += 1
        new.append(line)
        if i in fence_idx and kinds[i] == 'close':
            if i + 1 < len(out) and out[i + 1].strip() and '\x00' not in out[i + 1]:
                new.append('')
                stats['fence_blank_added'] += 1
    # 3) 裸围栏（无语言）自动补全
    content = {}
    open_idx = None
    lang_of = {}
    for i, line in enumerate(new):
        s = line.lstrip()
        if open_idx is not None and i not in fence_idx:
            content[open_idx].append(line)
            continue
        s2 = line.lstrip()
        m = re.match(r'^`{3,}(.*)$', s2)
        if not m:
            continue
        if i in fence_idx and kinds[i] == 'open':
            open_idx = i
            lang_of[i] = m.group(1).strip()
            content[i] = []
        elif open_idx is not None and re.match(r'^`{3,}\s*$', s2):
            open_idx = None
    for i, lang in list(lang_of.items()):
        if lang:
            continue
        detected = detect_lang('\n'.join(content.get(i, [])))
        if detected:
            pad = len(new[i]) - len(new[i].lstrip())
            new[i] = ' ' * pad + '```' + detected
            stats['lang_added'] += 1
            nlines = len(content.get(i, []))
            _sample('lang', path, new[i][:4] + '   (' + str(nlines) + ' 行裸代码)',
                    new[i] + '  [判定 ' + detected + ']')
    return new

def detect_lang(body):
    low = body.lower()
    if any(m in low for m in ('#include', 'int main', 'printf', 'scanf', 'malloc',
                              'struct ', 'typedef ', 'elemtype', 'lnode', 'bitree',
                              'vector<', 'cout', 'cin', 'std::')):
        return 'cpp'
    if any(m in low for m in ('def ', 'import ', 'print(')):
        return 'python'
    if any(m in low for m in ('<html', '<div', '<!doctype')):
        return 'html'
    return ''

# ---------------------------------------------------------------- protection
FENCE_BLOCK_RE = re.compile(r'^[ \t]*```[^\n]*\n[\s\S]*?^[ \t]*```[ \t]*$', re.M)
INLINE_CODE_RE = re.compile(r'`{1,3}[^`\n]*`{1,3}')
WIKILINK_RE = re.compile(r'!?\[\[[^\[\]\n]+\]\]')
MD_LINK_RE = re.compile(r'\[[^\[\]\n]*\]\([^)\n]+\)')
URL_RE = re.compile(r'(https?://[^\s)\]\u3000]+)')
FRONTMATTER_RE = re.compile(r'\A---\n[\s\S]*?\n---\n?')

def _unclosed_tail(lines):
    """找到未闭合围栏的起点行，返回 None 或 body 文本（从该行到文件尾）。"""
    inside = False
    start = None
    for i, line in enumerate(lines):
        s = line.lstrip()
        m = re.match(r'^`{3,}(.*)$', s)
        if not m:
            continue
        rest = m.group(1).strip()
        if not inside:
            if rest == '' or re.match(r'^[\w+\-._]+$', rest):
                inside = True
                start = i
        else:
            if re.match(r'^`{3,}\s*$', s):
                inside = False
                start = None
    if inside and start is not None:
        return line_idx_to_text(lines, start)
    return None

def line_idx_to_text(lines, start):
    return '\n'.join(lines[start:]) + '\n'

def protect_markup(text):
    # YAML frontmatter
    m = FRONTMATTER_RE.match(text)
    if m:
        text = _protect(m.group(0)) + text[m.end():]
    # 已闭合围栏代码块
    text = FENCE_BLOCK_RE.sub(lambda mm: _protect(mm.group(0)), text)
    # 未闭合围栏：其后的文本视同代码体一并隔离（避免后续正则误改）
    kick = '\n'.join(text.split('\n'))
    ulines = text.split('\n')
    tail = _unclosed_tail(ulines)
    if tail and '\x00' not in text[max(0, len(text) - len(tail)):]:
        pass
    if tail is not None:
        idx = text.find(tail.split('\n')[0])
        # 精确定位：未闭合围栏起点行首
        head = tail[:-1]
        pos = text.rfind(head)
        if pos != -1:
            text = text[:pos] + _protect(text[pos:])
    # 行内代码
    text = INLINE_CODE_RE.sub(lambda m: _protect(m.group(0)), text)
    # 双链 / 嵌入 / PDF++ 坐标链接
    text = WIKILINK_RE.sub(lambda m: _protect(m.group(0)), text)
    # markdown 链接与裸 URL
    text = MD_LINK_RE.sub(lambda m: _protect(m.group(0)), text)
    text = URL_RE.sub(lambda m: _protect(m.group(0)), text)
    return text

# ---------------------------------------------------------------- math fixes
INLINE_PAREN_RE = re.compile(r'\\\((.+?)\\\)', re.S)
DISPLAY_BRACKET_RE = re.compile(r'\\\[([\s\S]+?)\\\]')
CJK_PUNCT_SET = '。．，、；：！？'
DOLLAR_ESC_SCAN = re.compile(r'\$')

def is_mathy(content):
    if not content or len(content) > 300:
        return False
    if not content.strip():
        return False
    if re.search(r'[\u4e00-\u9fff]|[\u3000-\u303f]|[\uff01-\uff5e]', content):
        return False
    return bool(re.search(r'[A-Za-z\\^_{}=+\-*/<>]', content))

def fix_math(text, path):
    # YAML 保护头拼回时保持一致
    # a) \( \) → $...$
    def _paren(mm):
        stats['paren_to_dollar'] += 1
        before = mm.group(0)
        after = '$' + mm.group(1).strip() + '$'
        _sample('\\(\\)', path, before, after)
        return after
    text = INLINE_PAREN_RE.sub(_paren, text)
    # b) \[ \] → 独立成行的 $$ 块
    def _bracket(mm):
        stats['bracket_to_block'] += 1
        before = mm.group(0)
        after = '\n\n$$\n' + mm.group(1).strip().strip('\n') + '\n$$\n\n'
        _sample('\\[\\]', path, before, after)
        return after
    text = DISPLAY_BRACKET_RE.sub(_bracket, text)
    # c) $$ 块规范化（多行 / 含 \begin）
    text = normalize_block_dollars(text, path)
    # d) 行内 $ 内侧空格剥离 + 中文标点外移
    text = fix_inline_dollars(text, path)
    return text

def normalize_block_dollars(text, path):
    positions = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '\\' and i + 1 < n:
            i += 2
            continue
        if ch == '$' and i + 1 < n and text[i + 1] == '$':
            positions.append(i)
            i += 2
            continue
        i += 1
    odd = len(positions) % 2 != 0
    if odd:
        print('  警告: {} 中 $$ 出现奇数次({})，跳过块级规范化'.format(path, len(positions)))
        positions = positions[:len(positions) - 1]
    for k in range(len(positions) - 2, -1, -2):
        p1, p2 = positions[k], positions[k + 1]
        content = text[p1 + 2:p2]
        if '\n' not in content and '\\begin{' not in content:
            continue
        s_idx = text.rfind('\n', 0, p1) + 1
        e_pos = text.find('\n', p2)
        seg_end = e_pos + 1 if e_pos != -1 else len(text)
        prefix = text[s_idx:p1]
        suffix = text[p2 + 2:seg_end - 1 if e_pos != -1 else len(text)]
        body = content.strip('\n').rstrip()
        if body == '':
            continue
        need_prefix_nl = bool(prefix.strip())
        need_blank_before = need_prefix_nl or (s_idx > 0 and text[:s_idx - 1].rsplit('\n', 1)[-1].strip() != '')
        tail_after = text[seg_end:] if e_pos != -1 else ''
        nxt = tail_after.split('\n', 1)[0] if tail_after.startswith('\n') else ''
        need_blank_after = suffix.strip() != '' or bool(nxt.strip())

        new_seg = ''
        if prefix.strip():
            new_seg += prefix.rstrip() + '\n\n'
        elif need_blank_before:
            new_seg += '\n'
        if suffix.strip():
            new_seg += suffix.rstrip() + '\n\n'
        new_seg += '$$\n' + body + '\n$$\n'
        if suffix.strip() or need_blank_after:
            pass
        tail_rest = ''
        if suffix.strip():
            pass
        if nxt.strip():
            new_seg += '\n'
        replacement_ok = new_seg != text[s_idx:seg_end]
        if not replacement_ok:
            continue
        text = text[:s_idx] + new_seg + text[seg_end:]
        stats['block_dollar_norm'] += 1
        _sample('$$块', path,
                (prefix.strip() or '') + '$$' + body[:40] + '$$',
                '$$\\n' + body[:40] + '\\n$$  (独立成行)')
    return text

def _strip_cjk_tail(content):
    moved = ''
    while content and content[-1] in CJK_PUNCT_SET:
        moved = content[-1] + moved
        content = content[:-1]
    return content, moved

def fix_inline_dollars(text, path):
    out = []
    for line in text.split('\n'):
        if '$$' in line or '$' not in line or '\x00' in line:
            out.append(line)
            continue
        pos = []
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == '\\':
                i += 2
                continue
            if ch == '$':
                pos.append(i)
            i += 1
        if len(pos) < 2 or len(pos) % 2 != 0:
            out.append(line)
            continue
        pieces = []
        cursor = 0
        touched = False
        for k in range(0, len(pos) - 1, 2):
            a, b = pos[k], pos[k + 1]
            raw = line[a + 1:b]
            stripped, moved = _strip_cjk_tail(raw)
            inner = stripped.strip()
            newseg = '$' + inner + '$' + moved
            if newseg != '$' + raw + '$' and inner and is_mathy(inner):
                oldseg = '$' + raw + '$'
                stats['inline_trim'] += 1
                touched = True
                _sample('行内$', path, oldseg, newseg)
            pieces.append(line[cursor:a])
            pieces.append(newseg)
            cursor = b + 1
        pieces.append(line[cursor:])
        out.append(''.join(pieces) if touched else line)
    return '\n'.join(out)

# ---------------------------------------------------------------- structure
HEADING_START_RE = re.compile(r'^\s*#{1,6}\s')
INLINE_SPLIT_RE = re.compile(r'^(.{1,200}?[^\s])\s+(#{2,6}\s+[^\s].*)$')
LIST_FIX_RE = re.compile(r'^(\s*)-(?![\s\-\d])(.*)$')

def fix_structure(text, path):
    lines = text.split('\n')
    in_math = [False] * len(lines)
    inside = False
    for i, line in enumerate(lines):
        if re.match(r'^\s*\$\$\s*$', line):
            inside = not inside
            in_math[i] = True
            continue
        in_math[i] = inside
    out = []
    for i, line in enumerate(lines):
        mathline = in_math[i]
        if '\x00' not in line and not mathline:
            # -- 行中标题粘连拆分 --
            ms = INLINE_SPLIT_RE.match(line)
            if ms and len(ms.group(2)) < 80 and not HEADING_START_RE.match(line):
                stats['heading_inline_split'] += 1
                _sample('标题', path, line, ms.group(1).rstrip() + ' ⏎ ' + ms.group(2).strip())
                out.append(ms.group(1).rstrip())
                out.append(ms.group(2).strip())
                continue
            # -- 标题 # 后补空格 --
            mh = re.match(r'^(\s*)(#{1,6})([^\s#].*)$', line)
            if mh and (len(mh.group(2)) >= 2 or re.match(r'\d', mh.group(3))):
                newline = mh.group(1) + mh.group(2) + ' ' + mh.group(3)
                stats['heading_space'] += 1
                _sample('标题', path, line, newline)
                line = newline
            # -- 列表 "-内容" 补空格 --
            ml = LIST_FIX_RE.match(line)
            if ml and not HEADING_START_RE.match(line):
                newline = ml.group(1) + '- ' + ml.group(2)
                stats['list_space'] += 1
                _sample('列表', path, line, newline)
                line = newline
        # -- 标题上方补空行 --
        if HEADING_START_RE.match(line) and not (in_math[i] or '\x00' in line) and out:
            prev = out[-1]
            if prev.strip() and '\x00' not in prev and not HEADING_START_RE.match(prev) \
                    and not re.match(r'^\s*\$\$', prev):
                out.append('')
                stats['heading_blank'] += 1
        out.append(line)
    lines = out

    # -- 表格上方补空行 --
    def is_table_row(ln):
        return '\x00' not in ln and ln.lstrip().startswith('|') and ln.count('|') >= 2
    res = []
    n = len(lines)
    for i, line in enumerate(lines):
        if is_table_row(line) and res and res[-1].strip() and '\x00' not in res[-1]:
            if i + 1 < n and is_table_row(lines[i + 1]):
                res.append('')
                stats['table_blank'] += 1
        res.append(line)
    text = '\n'.join(res)
    return text

BLANK_RUN_RE = re.compile(r'\n{4,}')

def collapse_blanks(text):
    def repl(m):
        blanks = m.group(0).count('\n') - 1
        if blanks >= 3:
            stats['blank_collapsed'] += blanks - 2
        return '\n\n\n'
    return BLANK_RUN_RE.sub(repl, text)

# ---------------------------------------------------------------- pipeline
def process_text(orig, relpath, had_crlf):
    text = orig.replace('\r\n', '\n')
    # 1) 围栏粘连 / 空行 / 语言
    text = '\n'.join(fix_fences(text.split('\n'), relpath))
    # 保护隔离
    text = protect_markup(text)
    # 2) 数学
    text = fix_math(text, relpath)
    # 3) 结构
    text = fix_structure(text, relpath)
    # 4) 还原 + 空行压缩
    text = _restore(text)
    text = collapse_blanks(text)
    # 末尾换行保持
    if orig.endswith('\n') and not text.endswith('\n'):
        text += '\n'
    if had_crlf:
        text = text.replace('\n', '\r\n')
    return text

def find_md_files():
    found = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in sorted(dirnames) if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.lower().endswith('.md') and fn not in SKIP_FILES:
                found.append(os.path.join(dirpath, fn))
    return sorted(found)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    files = find_md_files()
    scanned = changed = 0
    for path in files:
        rel = os.path.relpath(path, ROOT)
        with open(path, 'rb') as f:
            raw = f.read()
        bom = raw.startswith(b'\xef\xbb\xbf')
        enc = 'utf-8-sig' if bom else 'utf-8'
        try:
            original = raw.decode('utf-8-sig' if bom else 'utf-8')
        except UnicodeDecodeError:
            try:
                original = raw.decode('gbk')
                enc = 'gbk'
            except UnicodeDecodeError:
                print('  跳过（非 UTF-8）: ' + rel)
                continue
        scanned += 1
        had_crlf = '\r\n' in original
        try:
            fixed = process_text(original, rel, had_crlf)
        except Exception as exc:
            import traceback
            print('  异常跳过 {}: {}'.format(rel, exc))
            traceback.print_exc()
            continue
        if fixed != original:
            changed += 1
            if not args.dry_run:
                with open(path, 'w', encoding=enc, newline='') as f:
                    f.write(fixed)

    print('=' * 62)
    print('全库显示问题扫描报告  ' + ('（dry-run 预览，未写入文件）' if args.dry_run else ''))
    print('=' * 62)
    total_code = stats['fence_glue_split'] + stats['lang_added'] + stats['fence_blank_added']
    total_math = stats['paren_to_dollar'] + stats['bracket_to_block'] + \
                 stats['block_dollar_norm'] + stats['inline_trim']
    total_struct = stats['heading_space'] + stats['heading_blank'] + \
                   stats['heading_inline_split'] + stats['table_blank'] + stats['list_space']
    print('共扫描: {} 个文件    共修改: {} 个文件'.format(scanned, changed))
    print()
    print('数学公式修复   共 {} 处'.format(total_math))
    print('    \\( \\) → $...$              : {}'.format(stats['paren_to_dollar']))
    print('    \\[ \\] → $$ 独立成行         : {}'.format(stats['bracket_to_block']))
    print('    块级 $$ 规范独立成行          : {}'.format(stats['block_dollar_norm']))
    print('    行内 $ 剥空格/标点外移        : {}'.format(stats['inline_trim']))
    print('代码块修复     共 {} 处'.format(total_code))
    print('    围栏粘连拆分                : {}'.format(stats['fence_glue_split']))
    print('    裸围栏自动补语言             : {}'.format(stats['lang_added']))
    print('    围栏前后补空行              : {}'.format(stats['fence_blank_added']))
    print('表格/标题/列表 共 {} 处'.format(total_struct))
    print('    表格上方补空行               : {}'.format(stats['table_blank']))
    print('    标题 # 后补空格              : {}'.format(stats['heading_space']))
    print('    标题上方补空行               : {}'.format(stats['heading_blank']))
    print('    行中标题粘粘连拆分            : {}'.format(stats['heading_inline_split']))
    print('    列表 "- " 补空格             : {}'.format(stats['list_space']))
    print('冗余空行压缩   {} 处'.format(stats['blank_collapsed']))
    print()
    print('典型修改示例（最多 10 处）：')
    for cat, pat, before, after in samples[:10]:
        print('  [{}] {}'.format(cat, pat))
        print('      - {}'.format(before))
        print('      + {}'.format(after))
    return 0

if __name__ == '__main__':
    sys.exit(main())
