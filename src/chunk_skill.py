#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chunk_skill.py —— 通用技能切块工具（内容无关，纯机械切分）

把一个技能目录里的 Markdown 按标题层级切成 300–1500 字的块，并生成
index.json / profile.json / system.md 骨架，供 PromptSmith 之类的程序加载。

用法：
  python chunk_skill.py <源目录> <输出目录> [选项]

选项：
  --min 300          块最小字数（小于则并入相邻块）
  --max 1500         块最大字数（超过则继续按下级标题切）
  --entry SKILL.md   入口文件名（只做记录，不做内容改写）
  --format fields|oneline|paragraph   输出形态，写进 profile.json
  --name 名字        规则集 id（默认取输出目录名）
  --dry              只统计不写盘

特点：
  - 只读源目录，绝不写入源目录
  - 不改写、不总结、不删减任何正文；拼接只在块间插空行
  - 文件名清洗非法字符；UTF-8 无 BOM
"""
import argparse
import hashlib
import json
import os
import re
import sys

ILLEGAL = re.compile(r'[:\\/*?"<>|]')
HEAD = re.compile(r'^(#{1,6})\s+(.*)$')


def clean_name(s, limit=48):
    s = ILLEGAL.sub('', s).strip().replace('\n', ' ')
    s = re.sub(r'\s+', ' ', s)
    return s[:limit] or 'untitled'


def read(p):
    with open(p, encoding='utf-8', errors='ignore') as f:
        return f.read()


def split_sections(text):
    """按标题切成 [(level, title, body)]，正文不丢。"""
    lines = text.splitlines()
    out, cur_title, cur_level, buf = [], None, 0, []
    for ln in lines:
        m = HEAD.match(ln)
        if m:
            if cur_title is not None or ''.join(buf).strip():
                out.append((cur_level, cur_title or '', '\n'.join(buf).strip()))
            cur_level, cur_title, buf = len(m.group(1)), m.group(2).strip(), []
        else:
            buf.append(ln)
    if cur_title is not None or ''.join(buf).strip():
        out.append((cur_level, cur_title or '', '\n'.join(buf).strip()))
    return out


def pack(sections, lo, hi):
    """贪心装箱：同文件内按顺序合并到 [lo, hi]，超 hi 单独立块。"""
    chunks, buf, buf_len = [], [], 0
    for lvl, title, body in sections:
        piece = (f"\n\n{'#' * max(2, min(lvl, 4))} {title}\n\n" if title else "\n\n") + body
        n = len(piece.strip())
        if n >= hi:
            if buf:
                chunks.append(buf); buf, buf_len = [], 0
            chunks.append([(title, piece)]); continue
        if buf_len + n > hi and buf:
            chunks.append(buf); buf, buf_len = [], 0
        buf.append((title, piece)); buf_len += n
    if buf:
        chunks.append(buf)
    # 余料过小的并入前一块
    merged = []
    for c in chunks:
        if merged and sum(len(p.strip()) for _, p in c) < lo:
            merged[-1].extend(c)
        else:
            merged.append(c)
    return merged


CJK = re.compile(r'[\u4e00-\u9fa5]{2,6}')
TICK = re.compile(r'`([^`\n]{2,24})`')
WORD = re.compile(r'\b[a-zA-Z][a-zA-Z_\-]{2,20}\b')
STOP = set('这个 那个 我们 你们 他们 可以 需要 如果 就是 什么 怎么 一个 没有 不是 已经 时候 因为 所以 但是 '
           '而且 或者 以及 这些 那些 自己 这样 那样'.split())


def keywords(text, k=6):
    cnt = {}
    for w in TICK.findall(text):
        cnt[w.strip()] = cnt.get(w.strip(), 0) + 3
    for w in CJK.findall(text):
        if w not in STOP:
            cnt[w] = cnt.get(w, 0) + 1
    for w in WORD.findall(text):
        cnt[w] = cnt.get(w, 0) + 1
    top = sorted(cnt.items(), key=lambda x: -x[1])[:k]
    return [w for w, _ in top]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src'); ap.add_argument('out')
    ap.add_argument('--min', type=int, default=300)
    ap.add_argument('--max', type=int, default=1500)
    ap.add_argument('--entry', default='SKILL.md')
    ap.add_argument('--format', default='fields')
    ap.add_argument('--name', default='')
    ap.add_argument('--dry', action='store_true')
    a = ap.parse_args()

    src, out = os.path.abspath(a.src), os.path.abspath(a.out)
    if not os.path.isdir(src):
        sys.exit(f'源目录不存在: {src}')
    kbdir = os.path.join(out, 'kb')
    name = a.name or os.path.basename(out)

    files = []
    for root, dirs, fs in os.walk(src):
        dirs[:] = [d for d in dirs if not os.path.join(root, d).startswith(out)]
        for f in fs:
            if f.lower().endswith('.md'):
                p = os.path.join(root, f)
                if p.startswith(out):
                    continue
                files.append(p)
    files.sort()
    if not files:
        sys.exit('源目录里没有 .md')

    if not a.dry:
        os.makedirs(kbdir, exist_ok=True)

    index, gi, total, sizes = [], 0, 0, []
    for p in files:
        rel = os.path.relpath(p, src).replace('\\', '/')
        text = read(p)
        secs = split_sections(text)
        if not secs:
            continue
        for group in pack(secs, a.min, a.max):
            gi += 1
            body = ''.join(x[1] for x in group).strip()
            if not body:
                continue
            title = (group[0][0] or rel).strip()
            if len(group) > 1:
                title = f'{title}（合并{len(group)}节）' if False else title
            chars = len(re.sub(r'\s', '', body))
            fname = f'{gi:03d}-{clean_name(title)}.md'
            head = f'# {title}\n\n> 来源：{rel}'
            content = head + '\n\n' + body + '\n'
            if not a.dry:
                with open(os.path.join(kbdir, fname), 'w', encoding='utf-8', newline='\n') as f:
                    f.write(content)
            index.append({
                'id': f'k{gi:03d}',
                'title': title,
                'file': f'kb/{fname}',
                'chars': chars,
                'keywords': keywords(body),
                'aliases': [],
                'src': rel,
                'hash': hashlib.md5(body.encode('utf-8')).hexdigest()[:8],
            })
            total += chars
            sizes.append(chars)

    if not sizes:
        sys.exit('没切出任何块')

    if not a.dry:
        with open(os.path.join(out, 'index.json'), 'w', encoding='utf-8', newline='\n') as f:
            json.dump({'ruleset': name, 'blocks': index}, f, ensure_ascii=False, indent=1)
        if not os.path.exists(os.path.join(out, 'aliases.json')):
            with open(os.path.join(out, 'aliases.json'), 'w', encoding='utf-8', newline='\n') as f:
                json.dump({}, f, ensure_ascii=False, indent=1)
        prof = os.path.join(out, 'profile.json')
        if not os.path.exists(prof):
            with open(prof, 'w', encoding='utf-8', newline='\n') as f:
                json.dump({
                    'id': name, 'display_name': name, 'target_model': '', 'target_kind': 'image',
                    'output_format': a.format, 'rules_file': a.entry, 'kb_dir': 'kb',
                    'budget': {'default_chunks': 6, 'min_chunks': 2, 'max_chunks': 10,
                               'max_inject_chars': 15000, 'hard_ceiling_chars': 24000,
                               'token_ratio': 0.7, 'safety_tokens': 2000},
                    'select': {'mode': 'A', 'keyword_weight': 0.65, 'alias_weight': 0.25, 'vector_weight': 0.10},
                    'generate': {'temperature': 0.75, 'top_p': 0.9, 'max_output_tokens': 4096,
                                 'enable_thinking': False},
                    'validate': {'must_start_with': '', 'ban_light_words': [], 'unique_fields': []},
                }, f, ensure_ascii=False, indent=1)

    sizes.sort()
    print(f'源目录   : {src}')
    print(f'输出目录 : {out}' + ('  [dry-run，未写盘]' if a.dry else ''))
    print(f'源文件数 : {len(files)}')
    print(f'块数     : {len(index)}')
    print(f'总字数   : {total}')
    print(f'平均/中位: {total // len(index)} / {sizes[len(sizes) // 2]}')
    print(f'最小/最大: {sizes[0]} / {sizes[-1]}')
    print(f'区间外   : <{a.min} = {sum(1 for s in sizes if s < a.min)} 个, >{a.max} = {sum(1 for s in sizes if s > a.max)} 个')


if __name__ == '__main__':
    main()
