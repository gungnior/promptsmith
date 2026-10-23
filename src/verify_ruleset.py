#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规则集校验工具（零依赖）。

用法：
    python src/verify_ruleset.py <规则集目录>
    python src/verify_ruleset.py <规则集目录> --strict    # 把 WARN 也当失败

检查项：
  1. profile.json 存在、必需键齐全、取值合法
  2. output_format 与 fields 自洽
  3. 入口（rules_file / resident_blocks）存在且长度合规
  4. 输出契约是否出现在入口前 2000 字内
  5. kb/ 文件名、字数区间、首行标题
  6. index.json 与磁盘是否一致
  7. aliases.json 是否有悬空引用
  8. 能否被程序实际加载并检索
"""
import json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

OK, WARN, FAIL = 'PASS', 'WARN', 'FAIL'
R = []


def rec(level, item, msg):
    R.append((level, item, msg))


def chars_of(text):
    return len(re.sub(r'\s', '', text))


def read(p):
    with open(p, encoding='utf-8', errors='ignore') as f:
        return f.read()


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    strict = '--strict' in sys.argv
    if not args:
        print(__doc__)
        return 2
    d = os.path.abspath(args[0])
    if not os.path.isdir(d):
        print(f'目录不存在：{d}')
        return 2
    print(f'校验规则集：{d}\n' + '=' * 62)

    # ---------- 1. profile.json ----------
    pf = os.path.join(d, 'profile.json')
    prof = {}
    if not os.path.exists(pf):
        rec(FAIL, 'profile.json', '缺失。程序会走"自动推断"，字段与校验都会退化')
    else:
        try:
            prof = json.load(open(pf, encoding='utf-8'))
            rec(OK, 'profile.json', f'可解析，{len(prof)} 个键')
        except Exception as e:
            rec(FAIL, 'profile.json', f'JSON 解析失败：{e}')
            prof = {}

    fmt = prof.get('output_format')
    if not fmt:
        rec(WARN, 'output_format', '未声明，运行时按 auto 处理（不校验结构）')
    elif fmt not in ('fields', 'oneline', 'paragraph', 'auto'):
        rec(FAIL, 'output_format', f'非法值 {fmt!r}，应为 fields/oneline/paragraph/auto')

    fields = prof.get('fields') or []
    fkeys = [(f.get('key') or f.get('name')) if isinstance(f, dict) else str(f) for f in fields]
    fkeys = [k for k in fkeys if k]
    if fmt == 'fields':
        if not fkeys:
            rec(FAIL, 'fields', 'output_format=fields 但 fields 为空 → 校验会退化成只查一个字段')
        else:
            # 字段名可以是短 tag（q/a/c），也可以是长蛇形名（subject_definitions）
            bad = [k for k in fkeys if not re.fullmatch(r'[a-z][a-z0-9_]{0,39}', k)]
            if bad:
                rec(WARN, 'fields', f'字段名不是 1-3 个小写字母：{bad}（模型输出行首需与之一致）')
            else:
                rec(OK, 'fields', f'{len(fkeys)} 个字段：{",".join(fkeys)}')
            dup = [k for k in set(fkeys) if fkeys.count(k) > 1]
            if dup:
                rec(FAIL, 'fields', f'字段重复：{dup}')
    elif fmt in ('oneline', 'paragraph'):
        rec(OK, 'fields', f'{fmt} 模式不需要 fields')

    b = prof.get('budget') or {}
    dflt, mx = b.get('default_chunks'), b.get('max_chunks')
    if dflt and mx and dflt > mx:
        rec(FAIL, 'budget', f'default_chunks({dflt}) > max_chunks({mx})')
    cap, hard = b.get('max_inject_chars'), b.get('hard_ceiling_chars')
    if cap and hard and cap > hard:
        rec(FAIL, 'budget', f'max_inject_chars({cap}) > hard_ceiling_chars({hard})')

    if 'validate' in prof:
        v = prof['validate']
        if v.get('regex'):
            try:
                re.compile(v['regex'])
                rec(OK, 'validate.regex', f'正则合法：{v["regex"][:40]}')
            except re.error as e:
                rec(FAIL, 'validate.regex', f'正则非法：{e}')

    # ---------- 2. 入口 ----------
    rf = prof.get('rules_file')
    rb = prof.get('resident_blocks') or []
    entry, entry_src = '', ''
    if rf and os.path.exists(os.path.join(d, rf)):
        entry, entry_src = read(os.path.join(d, rf)), rf
    elif rb:
        parts = []
        for bid in rb:
            hit = None
            for r, _, fs in os.walk(os.path.join(d, prof.get('kb_dir') or 'kb')):
                for f in fs:
                    if f.endswith('.md') and (bid in f or f.startswith(bid)):
                        hit = os.path.join(r, f)
                        break
            if hit:
                parts.append(read(hit))
        entry = '\n\n'.join(parts)
        entry_src = f'resident_blocks({len(rb)} 个)'
        if not entry:
            rec(FAIL, '入口', 'resident_blocks 指定的块一个都没找到')
    else:
        for cand in ('system.md', 'SKILL.md'):
            p = os.path.join(d, cand)
            if os.path.exists(p):
                entry, entry_src = read(p), cand
                break

    if not entry:
        rec(FAIL, '入口', '没有入口正文 → 运行时只能靠 80 字通用兜底，输出质量必然差')
    else:
        n = chars_of(entry)
        if n > 15000:
            rec(FAIL, '入口', f'{n} 字，超过 15000 上限（{entry_src}）')
        elif n < 1500:
            rec(WARN, '入口', f'仅 {n} 字，规则太少（{entry_src}）')
        else:
            rec(OK, '入口', f'{n} 字（{entry_src}）' + ('，建议 8000-13000' if n < 8000 else ''))

        head = entry[:2000]
        hit_fields = [k for k in fkeys if re.search(rf'\b{re.escape(k)}\b', head)]
        has_contract = bool(re.search(r'输出|契约|字段|格式', head))
        if fmt == 'fields' and fkeys:
            if len(hit_fields) >= max(1, len(fkeys) // 2):
                rec(OK, '契约位置', f'前 2000 字内出现 {len(hit_fields)}/{len(fkeys)} 个字段名')
            else:
                rec(FAIL, '契约位置', f'前 2000 字内只出现 {len(hit_fields)}/{len(fkeys)} 个字段名'
                                      '（契约必须在入口最前面，否则精简入口会丢格式）')
        elif has_contract:
            rec(OK, '契约位置', '前 2000 字内有格式/输出相关说明')
        else:
            rec(WARN, '契约位置', '前 2000 字内没找到"输出/格式/字段"字样，确认契约是否在开头')

    # ---------- 3. kb ----------
    kbd = os.path.join(d, prof.get('kb_dir') or 'kb')
    if not os.path.isdir(kbd):
        rec(WARN, 'kb', f'目录不存在：{kbd}')
        mds = []
    else:
        mds = sorted(f for f in os.listdir(kbd) if f.lower().endswith('.md'))
        if not mds:
            rec(FAIL, 'kb', '没有任何 .md 块')
        else:
            sizes, badname, notitle = [], [], []
            for f in mds:
                t = read(os.path.join(kbd, f))
                sizes.append((chars_of(t), f))
                if not re.match(r'^\d{2}-', f):
                    badname.append(f)
                if not re.match(r'^\s*#\s+\S', t):
                    notitle.append(f)
            tot = sum(s for s, _ in sizes)
            med = sorted(s for s, _ in sizes)[len(sizes) // 2]
            # 规则集可以声明「原子块」上限：某些块（完整样例）拆开就废，允许更大
            bmax = int((prof.get('budget') or {}).get('block_max_chars') or 1500)
            big = [f'{f}({s})' for s, f in sizes if s > bmax]
            small = [f'{f}({s})' for s, f in sizes if s < 300]
            rec(OK, 'kb', f'{len(mds)} 块，共 {tot} 字，中位 {med} 字（单块上限 {bmax}）')
            if big:
                rec(WARN, 'kb-过大', f'{len(big)} 块超过 {bmax} 字：{", ".join(big[:4])}')
            if small:
                rec(WARN, 'kb-碎片', f'{len(small)} 块小于 300 字：{", ".join(small[:4])}')
            if badname:
                rec(WARN, 'kb-命名', f'{len(badname)} 个文件名不是 `{{组号}}-标题.md`：{", ".join(badname[:3])}')
            if notitle:
                rec(WARN, 'kb-标题', f'{len(notitle)} 个文件首行不是 `# 标题`：{", ".join(notitle[:3])}')

    # ---------- 4. index.json ----------
    ip = os.path.join(d, 'index.json')
    if os.path.exists(ip):
        try:
            raw = json.load(open(ip, encoding='utf-8'))
            blocks = raw['blocks'] if isinstance(raw, dict) else raw
            rec(OK, 'index.json', f'{len(blocks)} 条')
            miss_file = [x for x in blocks if not os.path.exists(os.path.join(d, str(x.get('file', ''))))]
            if miss_file:
                rec(FAIL, 'index-文件', f'{len(miss_file)} 条指向不存在的文件：'
                                        f'{", ".join(str(x.get("file")) for x in miss_file[:3])}')
            wrong = []
            for x in blocks[:40]:
                p = os.path.join(d, str(x.get('file', '')))
                if os.path.exists(p):
                    real = chars_of(re.sub(r'^#\s+.+$', '', read(p), count=1, flags=re.M))
                    if x.get('chars') and abs(real - int(x['chars'])) > max(30, int(x['chars']) * 0.15):
                        wrong.append(f'{x["file"]}(记{x["chars"]}/实{real})')
            if wrong:
                rec(WARN, 'index-字数', f'{len(wrong)} 条字数与磁盘偏差过大：{", ".join(wrong[:3])}')
            nofile = [x for x in blocks if not x.get('file')]
            if nofile:
                rec(FAIL, 'index-字段', f'{len(nofile)} 条缺 file')
            if len(mds) and len(blocks) != len(mds):
                rec(WARN, 'index-数量', f'index 有 {len(blocks)} 条，kb 目录有 {len(mds)} 个文件')
        except Exception as e:
            rec(FAIL, 'index.json', f'解析失败：{e}')
    else:
        rec(WARN, 'index.json', '缺失（程序会现场扫描生成，质量稍差）')

    # ---------- 5. aliases.json ----------
    ap = os.path.join(d, 'aliases.json')
    if os.path.exists(ap):
        try:
            raw = json.load(open(ap, encoding='utf-8'))
            amap = raw['map'] if isinstance(raw, dict) and isinstance(raw.get('map'), dict) else raw
            ids = {str(x.get('id')) for x in (blocks if os.path.exists(ip) else [])}
            files = set(mds)
            dangling = []
            for k, v in (amap or {}).items():
                for ref in (v if isinstance(v, list) else [v]):
                    if ids and ref not in ids and ref not in files:
                        dangling.append(f'{k}→{ref}')
            if dangling:
                rec(WARN, 'aliases', f'{len(dangling)} 条悬空引用：{", ".join(dangling[:3])}')
            else:
                rec(OK, 'aliases', f'{len(amap)} 个键，无悬空引用')
        except Exception as e:
            rec(FAIL, 'aliases.json', f'解析失败：{e}')
    else:
        rec(WARN, 'aliases', '缺失（口语说法会检索不到，可后补）')

    # ---------- 6. 实际加载 ----------
    try:
        import promptsmith as PS
        prof2 = dict(prof)
        prof2['dir'] = d
        prof2.setdefault('id', os.path.basename(d))
        prof2.setdefault('blocks', [])
        if not prof2['blocks']:
            prof2['blocks'] = PS._build_index(d, prof2)
        prof2['entry_text'] = entry
        prof2['entry_chars'] = len(entry)
        q = '测试检索'
        cands = PS.retrieve(prof2, q)
        rec(OK, '实际加载', f'{len(prof2["blocks"])} 块可索引，检索可用（"{q}" 命中 {len(cands)} 块）')
        try:
            bud = PS.budget(prof2)
            rec(OK, '预算计算', f'可用 {bud["avail_chars"]} 字 / {bud["avail_tokens"]} token')
        except Exception as e:
            rec(WARN, '预算计算', f'失败：{e}')
    except Exception as e:
        rec(FAIL, '实际加载', f'导入或加载失败：{e}')

    # ---------- 汇总 ----------
    print()
    for lv, item, msg in R:
        icon = {OK: '✅', WARN: '⚠️ ', FAIL: '❌'}[lv]
        print(f'{icon} [{item}] {msg}')
    nf = sum(1 for lv, _, _ in R if lv == FAIL)
    nw = sum(1 for lv, _, _ in R if lv == WARN)
    print('=' * 62)
    print(f'PASS {len(R) - nf - nw} 项 · WARN {nw} 项 · FAIL {nf} 项')
    if nf:
        print('结论：不合格，先修 FAIL 项再加载')
        return 1
    if strict and nw:
        print('结论：--strict 模式下 WARN 视为失败')
        return 1
    print('结论：可用' + ('（有 WARN，建议逐条看过）' if nw else '，全部通过'))
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
