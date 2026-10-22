#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PromptSmith 核心：规则集加载 + 检索 + 预算 + 生成 + 校验。零第三方依赖。"""
import hashlib, json, math, os, re, shutil, sys, threading, time, urllib.error, urllib.parse, urllib.request


def _frozen():
    return bool(getattr(sys, 'frozen', False))


def _base_dir():
    """只读资源根：PyInstaller 打包后是 _MEIPASS，源码模式是项目根。"""
    if _frozen():
        mp = getattr(sys, '_MEIPASS', '')
        if mp:
            return mp
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _user_dir():
    """可写数据根：打包后是 %APPDATA%/PromptSmith（卸载不丢、可写），源码模式同项目根。"""
    if _frozen():
        d = os.path.join(os.environ.get('APPDATA') or os.path.expanduser('~'), 'PromptSmith')
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
        return d
    return _base_dir()


ROOT = _base_dir()
UROOT = _user_dir()
CFG = os.path.join(ROOT, 'data', 'config.json')

CJK = re.compile(r'[\u4e00-\u9fa5]{2,6}')
TOK = re.compile(r'[a-zA-Z][a-zA-Z_\-]{2,20}')


# ---------------- 配置 ----------------
def _cfg_path():
    """config.json 位置：优先可写目录，没有则从资源目录复制一份过去。"""
    up = os.path.join(UROOT, 'data', 'config.json')
    if os.path.exists(up):
        return up
    if os.path.exists(CFG):
        try:
            os.makedirs(os.path.dirname(up), exist_ok=True)
            shutil.copy2(CFG, up)
            return up
        except OSError:
            return CFG
    return CFG


def load_cfg():
    with open(_cfg_path(), encoding='utf-8') as f:
        return json.load(f)


def save_cfg(cfg):
    p = _cfg_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def model_of(cfg, mid=None):
    mid = mid or cfg.get('active_model')
    for m in cfg['model_profiles']:
        if m['id'] == mid:
            return m
    return cfg['model_profiles'][0]


# ---------------- 规则集 ----------------
def _read(p):
    with open(p, encoding='utf-8', errors='ignore') as f:
        return f.read()


def _guess_profile(d, name):
    """没有 profile.json 时按形态 B/C 推断一份最小配置。"""
    return {
        'id': name, 'display_name': name, 'target_model': '', 'target_kind': 'image',
        'output_format': 'auto', 'rules_file': 'SKILL.md' if os.path.exists(os.path.join(d, 'SKILL.md')) else None,
        'resident_blocks': [], 'kb_dir': '',
        'budget': {'default_chunks': 6, 'min_chunks': 2, 'max_chunks': 10,
                   'max_inject_chars': 15000, 'hard_ceiling_chars': 24000,
                   'token_ratio': 0.7, 'safety_tokens': 2000},
        'select': {'mode': 'A', 'keyword_weight': 0.65, 'alias_weight': 0.25, 'vector_weight': 0.1},
        'generate': {'temperature': 0.75, 'top_p': 0.9, 'max_output_tokens': 4096, 'enable_thinking': False},
        'validate': {'must_start_with': '', 'ban_light_words': [], 'unique_fields': []},
    }


def _collect_md(d, skip=()):
    out = []
    for r, ds, fs in os.walk(d):
        ds[:] = [x for x in ds if os.path.join(r, x) not in skip]
        for f in fs:
            if f.lower().endswith('.md'):
                out.append(os.path.join(r, f))
    return sorted(out)


def _build_index(d, prof):
    """扫 kb 目录（或整个目录）生成块表。"""
    kb = os.path.join(d, prof.get('kb_dir') or '')
    base = kb if prof.get('kb_dir') and os.path.isdir(kb) else d
    blocks, i = [], 0
    for p in _collect_md(base):
        if os.path.basename(p) in ('SKILL.md', 'index.json'):
            continue
        if os.path.basename(p) in ('system.md',):
            continue
        txt = _read(p)
        title = ''
        m = re.search(r'^#\s+(.+)$', txt, re.M)
        if m:
            title = m.group(1).strip()
        title = title or os.path.splitext(os.path.basename(p))[0]
        body = re.sub(r'^#\s+.+$', '', txt, count=1, flags=re.M).strip()
        chars = len(re.sub(r'\s', '', body)) or 1
        i += 1
        blocks.append({
            'id': f'k{i:03d}', 'title': title,
            'file': os.path.relpath(p, d).replace('\\', '/'),
            'chars': chars, 'keywords': _kw(body), 'aliases': [], 'src': os.path.relpath(p, base).replace('\\', '/'),
        })
    return blocks


STOP = set('这个 那个 我们 你们 他们 可以 需要 如果 就是 什么 怎么 一个 没有 不是 已经 时候 因为 所以 但是'.split())


def _kw(text, k=6):
    c = {}
    for w in CJK.findall(text[:4000]):
        if w not in STOP:
            c[w] = c.get(w, 0) + 1
    for w in TOK.findall(text[:4000]):
        c[w] = c.get(w, 0) + 1
    return [w for w, _ in sorted(c.items(), key=lambda x: -x[1])[:k]]


def _is_ruleset_dir(d):
    return any(os.path.exists(os.path.join(d, x)) for x in ('profile.json', 'SKILL.md', 'index.json'))


def _materialize(root):
    """把位于只读资源目录（打包后的安装目录）下的规则集复制到可写目录，返回可写路径。
    - root 本身是规则集 → 复制为 <可写>/rulesets/<同名>
    - root 是容器目录（含多个规则集）→ 逐个复制其中的规则集，返回 <可写>/rulesets
    源码模式或本就可写时原样返回。"""
    if not _frozen():
        return root
    base = _base_dir()
    try:
        if os.path.commonpath([os.path.abspath(root), base]) != base:
            return root
    except ValueError:
        return root
    dst_root = os.path.join(UROOT, 'rulesets')
    try:
        if _is_ruleset_dir(root):
            dst = os.path.join(dst_root, os.path.basename(os.path.abspath(root)))
            if not os.path.exists(dst):
                shutil.copytree(root, dst)
        else:
            for name in sorted(os.listdir(root)):
                sub = os.path.join(root, name)
                if os.path.isdir(sub) and _is_ruleset_dir(sub):
                    dst = os.path.join(dst_root, name)
                    if not os.path.exists(dst):
                        shutil.copytree(sub, dst)
        if os.path.isdir(dst_root):
            return dst_root
    except OSError:
        pass
    return root


def _iter_ruleset_dirs(root, max_depth=2):
    """在 root 下找规则集目录：自身是 → [root]；否则向下找（含容器套容器，最多 max_depth 层）。"""
    if _is_ruleset_dir(root):
        yield root
        return
    seen = 0
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return
    for name in names:
        sub = os.path.join(root, name)
        if not os.path.isdir(sub):
            continue
        if _is_ruleset_dir(sub):
            seen += 1
            yield sub
        elif max_depth > 1 and not name.startswith(('.', '_')):
            for deep in _iter_ruleset_dirs(sub, max_depth - 1):
                seen += 1
                yield deep
    if not seen:
        return


def discover(cfg):
    """扫描 ruleset_roots，返回 [{...profile..., 'dir':..., 'entry':str, 'blocks':[...]}]
    相对路径基于可写目录解析；位于只读资源目录（安装目录）下的规则集会先复制到可写目录。"""
    found = []
    for root in cfg.get('ruleset_roots', []):
        root = str(root)
        if not os.path.isabs(root):
            cand = os.path.normpath(os.path.join(UROOT, root))
            if not os.path.isdir(cand):
                alt = os.path.normpath(os.path.join(ROOT, root))
                if os.path.isdir(alt):
                    cand = alt          # 可写目录没有，回退到资源目录（随后会复制过去）
            root = cand
        if not os.path.isdir(root):
            continue
        root = _materialize(root)
        if not os.path.isdir(root):
            continue
        # 收集规则集目录（自身是，或向下找，兼容容器套容器）
        cands = [(os.path.basename(os.path.abspath(d)), d)
                 for d in _iter_ruleset_dirs(root)]
        seen_dirs, uniq = set(), []
        for nm, d in cands:
            key = os.path.abspath(d)
            if key in seen_dirs:
                continue
            seen_dirs.add(key)
            uniq.append((nm, d))
        cands = uniq
        for name, d in cands:
            pf = os.path.join(d, 'profile.json')
            prof = json.load(open(pf, encoding='utf-8')) if os.path.exists(pf) else _guess_profile(d, name)
            prof['dir'] = d
            prof.setdefault('id', name)
            # 入口
            entry = prof.get('entry_text')
            if not entry:
                rf = prof.get('rules_file')
                for cand in ([rf] if rf else []) + ['system.md', 'SKILL.md']:
                    if cand and os.path.exists(os.path.join(d, cand)):
                        entry = _read(os.path.join(d, cand))
                        break
                if not entry:
                    entry = ''
            # 块
            idxp = os.path.join(d, 'index.json')
            blocks = None
            if os.path.exists(idxp):
                try:
                    raw = json.load(open(idxp, encoding='utf-8'))
                    blocks = raw['blocks'] if isinstance(raw, dict) else raw
                except Exception:
                    blocks = None
            if not blocks:
                blocks = _build_index(d, prof)
            for _b in blocks:
                if 'text' not in _b:
                    _fp = os.path.join(d, _b.get('file', ''))
                    _b['text'] = _read(_fp)[:6000].lower() if os.path.exists(_fp) else ''
            # 常驻块拼入口
            rb = prof.get('resident_blocks') or []
            if rb and not entry:
                bymap = {b['id']: b for b in blocks}
                parts = []
                for bid in rb:
                    b = bymap.get(bid)
                    if not b:
                        continue
                    p = os.path.join(d, b['file'])
                    if os.path.exists(p):
                        parts.append(_read(p))
                entry = '\n\n'.join(parts)
            prof['entry_text'] = entry
            prof['blocks'] = blocks
            prof['entry_chars'] = len(entry)
            found.append(prof)
    return found


# ---------------- 检索 ----------------
# 内置兜底词表：只放几条中性的，用于演示机制。
# 实际词表请放到 data/zh2tag.json（全局）或 rulesets/<规则集>/aliases.json（私有，优先级更高），
# 也可以在界面「检索词典」里直接增删，或用「联网更新」从 Danbooru 镜像抓官方中文名。
ZH2TAG = {
    '逆光': 'backlighting', '背光': 'backlighting', '日落': 'sunset', '阳光': 'sunlight',
    '夜景': 'night', '月光': 'moonlight', '特写': 'close-up', '半身': 'upper body',
    '全身': 'full body', '双人': '2girls', '雨天': 'rain', '剪影': 'silhouette',
}


_ZH_CACHE = {}


def zh2tag():
    """全局中文词表：优先读可写目录的 data/zh2tag.json，再读资源目录，最后用内置兜底。"""
    if _ZH_CACHE:
        return _ZH_CACHE
    p = os.path.join(UROOT, 'data', 'zh2tag.json')
    if not os.path.exists(p):
        p = os.path.join(ROOT, 'data', 'zh2tag.json')
    d = {}
    if os.path.exists(p):
        try:
            raw = json.load(open(p, encoding='utf-8'))
            d = {k: v for k, v in raw.items() if not k.startswith('_') and isinstance(v, str)}
        except Exception:
            d = {}
    if not d:
        d = dict(ZH2TAG)
    # 合并联网词典里的官方中文名（只补没配过的）
    try:
        for tag, rec in load_tagdict().items():
            if tag.startswith('_') or not isinstance(rec, dict):
                continue
            for zh in (rec.get('zh') or []):
                if zh and zh not in d:
                    d[zh] = tag
    except Exception:
        pass
    _ZH_CACHE.update(d)
    return _ZH_CACHE


def save_zh2tag(d):
    p = os.path.join(UROOT, 'data', 'zh2tag.json')
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(d, open(p, 'w', encoding='utf-8', newline='\n'), ensure_ascii=False, indent=1)
    _ZH_CACHE.clear()
    _ZH_CACHE.update(d)


def _qwords(q, overrides=()):
    ws = set()
    for run in re.findall(r'[\u4e00-\u9fa5]+', q):
        for n in (2, 3, 4):
            for i in range(len(run) - n + 1):
                ws.add(run[i:i + n])
    for w in TOK.findall(q):
        ws.add(w.lower())
    for zh, en in zh2tag().items():
        if zh in q and zh not in overrides:
            ws.add(en)
    return [w for w in ws if w not in STOP]


def retrieve(prof, query, topn=None, aliases=None):
    sel = prof.get('select', {})
    w_kw = sel.get('keyword_weight', 0.65)
    w_al = sel.get('alias_weight', 0.25)
    topn = topn or prof['budget']['default_chunks']
    aliases = aliases if aliases is not None else load_aliases(prof)
    ws = _qwords(query, tuple(aliases.keys()))
    td = {}
    # 联网词典：已抓到的英文 tag 用 wiki 里的相关 tag 做弱扩展
    try:
        td = load_tagdict()
        if td:
            rel = []
            for w in ws:
                rec = td.get(w)
                if isinstance(rec, dict):
                    rel.extend([r for r in (rec.get('rel') or []) if r not in ws][:4])
            ws = ws + rel[:12]
    except Exception:
        pass
    ql = query.lower()
    blocks = prof['blocks']
    N = max(1, len(blocks))
    df, idf = {}, {}
    for w in ws:
        df[w] = sum(1 for b in blocks if w in (b.get('text') or '') or w in b['title'].lower())
        idf[w] = max(0.12, math.log((N + 1) / (df[w] + 1.0)))
    scored = []
    vac = vector_scores(prof, query)
    vw = sel.get('vector_weight', 0.35)
    for b in prof['blocks']:
        title = b['title'].lower()
        bid = str(b['id'])
        kws = [str(k).lower() for k in b.get('keywords', [])]
        body = b.get('text') or ''
        hit, ah, why = 0.0, 0, []
        for w in ws:
            iw = idf[w]
            rec = td.get(w)
            if isinstance(rec, dict) and rec.get('count', 0) > 20000:
                iw *= 1.12   # 主流 tag 微加权
            if w in title:
                hit += (2 + len(w)) * iw; why.append(w)
            elif any(w in k for k in kws):
                hit += (1 + len(w) * 0.5) * iw; why.append(w)
            elif w in bid.lower():
                hit += (1 + len(w) * 0.3) * iw; why.append(w)
            elif w in body:
                hit += (0.5 + len(w) * 0.4) * iw; why.append(w)
        for k, ids in aliases.items():
            if k.lower() in ql and (bid in ids or b['title'] in ids):
                ah += 1; why.append(k)
        base = (hit * w_kw + ah * w_al * 12) * (900.0 / (900.0 + b['chars']))
        cos = vac.get(b['id']) or 0.0
        if cos > 0.30:
            base += vw * (cos - 0.30) * 24
            if cos >= 0.45:
                why.append(f'向量{cos:.2f}')
        score = round(base, 3)
        if score > 0:
            scored.append((score, why, b))
    scored.sort(key=lambda x: -x[0])
    return [{'score': s, 'why': sorted(set(w)), **b} for s, w, b in scored[:max(topn, 1) * 3]]


def load_aliases(prof):
    p = os.path.join(prof['dir'], 'aliases.json')
    if os.path.exists(p):
        try:
            d = json.load(open(p, encoding='utf-8'))
            if isinstance(d, dict) and isinstance(d.get('map'), dict):
                return d['map']
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
    return {}


def search_content(prof, q, limit=40):
    """在块正文里全文搜关键词，返回命中片段，供用户手动添加块。"""
    q = (q or '').strip()
    if not q:
        return []
    ql = q.lower()
    terms = [t for t in _qwords(q) if len(t) >= 2]
    if ql not in terms:
        terms.append(ql)
    out = []
    for b in prof['blocks']:
        text = b.get('text') or ''
        if not text:
            fp = os.path.join(prof['dir'], b.get('file', ''))
            text = _read(fp).lower() if os.path.exists(fp) else ''
        hits, first = 0, -1
        for t in terms:
            c = text.count(t)
            if c:
                hits += c
                if first < 0:
                    first = text.find(t)
        if not hits:
            continue
        if first < 0:
            first = 0
        s = max(0, first - 40)
        snippet = text[s:s + 110].replace('\n', ' ')
        out.append({'id': b['id'], 't': b['title'], 'c': b['chars'],
                    'hits': hits, 'snippet': ('…' if s else '') + snippet + '…'})
    out.sort(key=lambda x: -x['hits'])
    return out[:limit]


def learn_alias(prof, query, block_id):
    """用户手动选块时记录「说法 → 块」。只记有效词，并有总量上限。"""
    p = os.path.join(prof['dir'], 'aliases.json')
    data = {}
    if os.path.exists(p):
        try:
            data = json.load(open(p, encoding='utf-8'))
        except Exception:
            data = {}
    amap = data['map'] if isinstance(data, dict) and isinstance(data.get('map'), dict) else data
    if not isinstance(amap, dict):
        amap = {}
    vocab = set(zh2tag().keys())
    for b in prof['blocks']:
        vocab.add(b['title'])
    cap = int((prof.get('budget') or {}).get('alias_max', 500))
    for w in _qwords(query):
        if not (2 <= len(w) <= 4) or w in STOP:
            continue
        if w.isascii():
            if len(w) < 3:
                continue
        elif not any(w in v for v in vocab):
            continue
        lst = amap.get(w)
        if not isinstance(lst, list):
            lst = []
        if block_id not in lst:
            lst.append(block_id)
            amap[w] = lst[-8:]
    if len(amap) > cap:
        keep = sorted(amap.items(), key=lambda kv: -len(kv[1]))[:cap]
        amap = dict(keep)
    if isinstance(data, dict) and 'map' in data:
        data['map'] = amap
    else:
        data = amap
    json.dump(data, open(p, 'w', encoding='utf-8', newline='\n'), ensure_ascii=False, indent=1)


def record_miss(query):
    """记录检索不到的输入词，供用户回头补词表。"""
    p = os.path.join(UROOT, 'data', 'misses.json')
    d = []
    if os.path.exists(p):
        try:
            d = json.load(open(p, encoding='utf-8'))
        except Exception:
            d = []
    q = (query or '').strip()[:80]
    if not q:
        return
    for x in d:
        if x.get('q') == q:
            x['n'] = x.get('n', 1) + 1
            x['ts'] = time.strftime('%Y-%m-%d %H:%M')
            break
    else:
        d.append({'q': q, 'n': 1, 'ts': time.strftime('%Y-%m-%d %H:%M')})
    d = sorted(d, key=lambda x: -x.get('n', 1))[:300]
    json.dump(d, open(p, 'w', encoding='utf-8', newline='\n'), ensure_ascii=False, indent=1)


def write_block(prof, block_id, text):
    """实时修改 md：先备份 .bak，再覆写，并刷新内存索引。"""
    for b in prof['blocks']:
        if b['id'] != block_id:
            continue
        p = os.path.join(prof['dir'], b['file'])
        if not os.path.exists(p):
            return False, '文件不存在'
        bak = p + '.bak'
        if not os.path.exists(bak):
            shutil.copy2(p, bak)
        with open(p, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        b['chars'] = len(re.sub(r'\s', '', text)) or 1
        b['text'] = text[:6000].lower()
        _ENTRY_CACHE.clear()
        return True, p
    return False, '块 id 不存在'


def restore_block(prof, block_id):
    for b in prof['blocks']:
        if b['id'] != block_id:
            continue
        p = os.path.join(prof['dir'], b['file'])
        bak = p + '.bak'
        if not os.path.exists(bak):
            return False, '没有备份'
        shutil.copy2(bak, p)
        t = _read(p)
        b['chars'] = len(re.sub(r'\s', '', t)) or 1
        b['text'] = t[:6000].lower()
        _ENTRY_CACHE.clear()
        return True, p
    return False, '块 id 不存在'


def add_ruleset_root(path):
    cfg = load_cfg()
    p = os.path.abspath(path)
    if not os.path.isdir(p):
        return False, '目录不存在'
    roots = cfg.get('ruleset_roots') or []
    # 已用相对路径记录过的就不再重复添加（相对路径基于可写目录）
    try:
        rel = os.path.relpath(p, UROOT)
    except ValueError:
        rel = p
    stored = rel if (not os.path.isabs(rel) and not rel.startswith('..')) else p
    if stored in roots or p in roots:
        return False, '已经在列表里'
    roots.append(stored)
    cfg['ruleset_roots'] = roots
    save_cfg(cfg)
    return True, stored


# ---------------- 收藏夹（磁盘持久化，换浏览器也在） ----------------
FAV_PATH = os.path.join(UROOT, 'cases', 'favorites.json')


def fav_list():
    if not os.path.exists(FAV_PATH):
        return []
    try:
        d = json.load(open(FAV_PATH, encoding='utf-8'))
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _fav_save(items):
    os.makedirs(os.path.dirname(FAV_PATH), exist_ok=True)
    json.dump(items[:500], open(FAV_PATH, 'w', encoding='utf-8', newline='\n'),
              ensure_ascii=False, indent=1)


def fav_add(item):
    items = fav_list()
    out = (item.get('out') or '').strip()
    if not out:
        return False, '没有可收藏的输出'
    rec = {
        'id': 'f' + str(int(time.time() * 1000)),
        'ts': time.strftime('%Y-%m-%d %H:%M'),
        'rs': item.get('rs') or '',
        'text': (item.get('text') or '')[:400],
        'out': out[:8000],
        'blocks': [b if isinstance(b, str) else (b.get('title') or '')
                   for b in (item.get('blocks') or [])][:20],
        'note': (item.get('note') or '')[:200],
    }
    # 同一输出不重复收藏
    for x in items:
        if x.get('out') == rec['out']:
            return True, '已经收藏过了'
    items.insert(0, rec)
    _fav_save(items)
    return True, '已收藏'


def fav_del(fid):
    items = fav_list()
    n = len(items)
    items = [x for x in items if x.get('id') != fid]
    _fav_save(items)
    return n != len(items)


# ---------------- 向量兜底（Ollama bge-m3，可选） ----------------
VEC_DIR = os.path.join(UROOT, 'vectors')
_VEC = {}          # ruleset id -> {'dim':int, 'vecs':{block_id:[float]}}
_VEC_PROG = {}     # ruleset id -> {'state','done','total','msg'}
_VEC_Q_CACHE = {}  # query -> vector
_VEC_LOCK = threading.Lock()


def vec_cfg():
    return (load_cfg().get('vector') or {})


def _safe_name(s):
    return re.sub(r'[^0-9A-Za-z_.\-]+', '_', str(s))[:60]


def _vec_path(prof):
    return os.path.join(VEC_DIR, _safe_name(prof['id']) + '.vec.json')


def embed_texts(texts):
    """调本地 Ollama 取句向量。返回 ([[float]], err)。强制 CPU 跑，避免和 27B 抢显存。"""
    c = vec_cfg()
    base = (c.get('ollama_base') or 'http://127.0.0.1:11434').rstrip('/')
    model = c.get('embed_model') or 'bge-m3-embed:latest'
    body = json.dumps({'model': model, 'input': list(texts),
                       'options': {'num_gpu': 0}}).encode()
    req = urllib.request.Request(base + '/api/embed', data=body,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=c.get('timeout_sec', 120)) as r:
            d = json.loads(r.read().decode())
        embs = d.get('embeddings') or []
        if embs:
            return embs, None
        return None, 'Ollama 返回空向量'
    except Exception as e:
        # 旧接口兜底（一次一条）
        try:
            out = []
            for t in texts:
                body = json.dumps({'model': model, 'prompt': t,
                                   'options': {'num_gpu': 0}}).encode()
                req = urllib.request.Request(base + '/api/embeddings', data=body,
                                             headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(req, timeout=c.get('timeout_sec', 120)) as r:
                    out.append(json.loads(r.read().decode())['embedding'])
            return out, None
        except Exception:
            return None, f'连不上 Ollama（{base}）：{e}'


def _block_text(prof, b, limit=700):
    t = b.get('text') or ''
    if not t:
        fp = os.path.join(prof['dir'], b.get('file', ''))
        t = _read(fp).lower() if os.path.exists(fp) else ''
    return (b['title'] + '\n' + t)[:limit]


def load_vectors(prof):
    rid = prof['id']
    if rid in _VEC:
        return _VEC[rid]
    p = _vec_path(prof)
    if not os.path.exists(p):
        return None
    try:
        raw = json.load(open(p, encoding='utf-8'))
    except Exception:
        return None
    vecs = {}
    for bid, item in (raw.get('blocks') or {}).items():
        # int8 压缩存储，读时还原
        vecs[bid] = [v / 100.0 for v in item.get('q') or []]
    d = {'dim': raw.get('dim'), 'vecs': vecs, 'model': raw.get('model'), 'built': raw.get('built')}
    with _VEC_LOCK:
        _VEC[rid] = d
    return d


def build_vectors(prof, force=False, batch=24):
    """为规则集建/增量更新向量缓存（后台线程调用）。"""
    rid = prof['id']
    total = len(prof['blocks'])
    _VEC_PROG[rid] = {'state': 'running', 'done': 0, 'total': total, 'msg': ''}
    p = _vec_path(prof)
    old = {}
    if os.path.exists(p) and not force:
        try:
            old = json.load(open(p, encoding='utf-8')).get('blocks') or {}
        except Exception:
            old = {}
    todo = []
    for b in prof['blocks']:
        txt = _block_text(prof, b)
        h = hashlib.md5(txt.encode('utf-8')).hexdigest()[:16]
        o = old.get(b['id'])
        if o and o.get('h') == h and o.get('q'):
            continue
        todo.append((b, txt, h))
    out = {bid: v for bid, v in old.items() if any(b['id'] == bid for b in prof['blocks'])}
    dim = None
    try:
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            embs, err = embed_texts([t for _, t, _ in chunk])
            if err:
                _VEC_PROG[rid] = {'state': 'error', 'done': i, 'total': total, 'msg': err}
                return False
            for (b, _, h), v in zip(chunk, embs):
                dim = len(v)
                out[b['id']] = {'h': h, 'q': [int(round(x * 100)) for x in v]}
            _VEC_PROG[rid] = {'state': 'running', 'done': min(total, len(out)),
                              'total': total, 'msg': f'已算 {len(out)}/{total}'}
        os.makedirs(VEC_DIR, exist_ok=True)
        json.dump({'model': (vec_cfg().get('embed_model') or 'bge-m3-embed:latest'),
                   'dim': dim, 'built': time.strftime('%Y-%m-%d %H:%M'),
                   'count': len(out), 'blocks': out},
                  open(p, 'w', encoding='utf-8', newline='\n'), ensure_ascii=False)
        with _VEC_LOCK:
            _VEC.pop(rid, None)
        _VEC_PROG[rid] = {'state': 'done', 'done': len(out), 'total': total,
                          'msg': f'完成 {len(out)} 块（新增 {len(todo)}）'}
        return True
    except Exception as e:
        _VEC_PROG[rid] = {'state': 'error', 'done': 0, 'total': total, 'msg': str(e)}
        return False


def vector_status(prof):
    rid = prof['id']
    p = _vec_path(prof)
    info = {'exists': os.path.exists(p), 'progress': _VEC_PROG.get(rid)}
    if os.path.exists(p):
        try:
            raw = json.load(open(p, encoding='utf-8'))
            info.update({'count': raw.get('count'), 'built': raw.get('built'),
                         'model': raw.get('model'), 'dim': raw.get('dim')})
        except Exception:
            pass
    info['enabled'] = bool(vec_cfg().get('enabled'))
    return info


def _cos(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    s = na = nb = 0.0
    for x, y in zip(a, b):
        s += x * y
        na += x * x
        nb += y * y
    if not na or not nb:
        return 0.0
    return s / math.sqrt(na * nb)


def query_vector(q):
    if q in _VEC_Q_CACHE:
        return _VEC_Q_CACHE[q]
    embs, err = embed_texts([q])
    if err or not embs:
        return None
    v = embs[0]
    if len(_VEC_Q_CACHE) > 40:
        _VEC_Q_CACHE.clear()
    _VEC_Q_CACHE[q] = v
    return v


def vector_scores(prof, query):
    """返回 {block_id: cos}；未启用/未建库时返回 {}。"""
    if not vec_cfg().get('enabled'):
        return {}
    d = load_vectors(prof)
    if not d or not d.get('vecs'):
        return {}
    qv = query_vector(query[:400])
    if not qv:
        return {}
    return {bid: _cos(qv, v) for bid, v in d['vecs'].items()}


# ---------------- 联网词典（Danbooru 镜像 safebooru.donmai.us，可选） ----------------
# 每 tag 抓三样：post_count（热度）、other_names 里的中文名、wiki 里的相关 tag。
# 只发送 tag 名，绝不发送用户正文。
TAGDICT_PATH = os.path.join(UROOT, 'data', 'tagdict.json')
DBU = 'https://safebooru.donmai.us'
_TD_CACHE = {}
_TAG_PROG = {}


def load_tagdict():
    if _TD_CACHE:
        return _TD_CACHE
    if os.path.exists(TAGDICT_PATH):
        try:
            d = json.load(open(TAGDICT_PATH, encoding='utf-8'))
            if isinstance(d, dict):
                _TD_CACHE.update(d)
        except Exception:
            pass
    return _TD_CACHE


def _norm_tag(t):
    t = str(t).strip().lower().replace(' ', '_')
    return re.sub(r'[^a-z0-9_\-]', '', t)


def _dget(path, timeout=12):
    req = urllib.request.Request(DBU + path, headers={'User-Agent': 'PromptSmith/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8', 'ignore'))


def fetch_taginfo(tags, prog=None):
    """增量抓取 tag 词典。返回统计。后台线程调用。"""
    d = load_tagdict()
    hit_cn = hit_hot = 0
    todo = [t for t in tags if not (d.get(_norm_tag(t)) or {}).get('zh')]
    for i, raw in enumerate(todo):
        t = _norm_tag(raw)
        if not t:
            continue
        rec = {'count': -1, 'zh': [], 'rel': []}
        try:
            arr = _dget('/tags.json?search%5Bname%5D=' + urllib.parse.quote(t) + '&limit=1')
            if arr and (arr[0].get('name') or '').lower() == t:
                rec['count'] = int(arr[0].get('post_count') or 0)
            time.sleep(0.05)
            wp = _dget('/wiki_pages.json?search%5Btitle%5D=' + urllib.parse.quote(t) + '&limit=1')
            if wp:
                w = wp[0]
                rec['zh'] = [x for x in (w.get('other_names') or [])
                             if re.search(r'[\u4e00-\u9fa5]', x)][:8]
                rec['rel'] = sorted({m.lower() for m in
                                     re.findall(r'\[\[([a-z][a-z_]{2,24})(?:\|[^\]]*)?\]\]',
                        w.get('body') or '')} - {t})[:12]
            time.sleep(0.05)
        except Exception:
            pass
        d[t] = rec
        if rec['zh']:
            hit_cn += 1
        if rec['count'] > 20000:
            hit_hot += 1
        if prog is not None and (i % 5 == 0 or i == len(todo) - 1):
            prog.update({'done': i + 1, 'total': len(todo), 'cn': hit_cn,
                         'msg': f'已查 {i + 1}/{len(todo)}，中文名 {hit_cn}'})
    d['_ts'] = time.strftime('%Y-%m-%d %H:%M')
    os.makedirs(os.path.dirname(TAGDICT_PATH), exist_ok=True)
    json.dump(d, open(TAGDICT_PATH, 'w', encoding='utf-8', newline='\n'),
              ensure_ascii=False, indent=1)
    _TD_CACHE.clear()
    _TD_CACHE.update(d)
    _ZH_CACHE.clear()
    if prog is not None:
        prog.update({'state': 'done', 'done': len(todo), 'total': len(todo),
                     'msg': f'完成 {len(todo)} 个 tag，中文名 {hit_cn}，高频 {hit_hot}'})
    return {'ok': True, 'fetched': len(todo), 'cn': hit_cn, 'hot': hit_hot}


def tagdict_stat():
    d = load_tagdict()
    tags = {k: v for k, v in d.items() if not k.startswith('_') and isinstance(v, dict)}
    return {'n': len(tags), 'cn': sum(1 for v in tags.values() if v.get('zh')),
            'ts': d.get('_ts', ''), 'prog': dict(_TAG_PROG)}


# ---------------- 预算 ----------------
def budget(prof):
    b = dict(prof['budget'])
    m = model_of(load_cfg())
    ctx = m.get('n_ctx', 65536)
    out = m.get('max_output_tokens', 4096)
    ratio = b.get('token_ratio', 0.7)
    overhead = 1200  # 系统/工具描述余量
    avail_tok = ctx - out - b.get('safety_tokens', 2000) - overhead - int((prof.get('entry_chars') or len(prof.get('entry_text') or '')) * ratio)
    avail_chars = max(2000, int(avail_tok / ratio))
    b['ctx'] = ctx
    b['avail_tokens'] = avail_tok
    b['avail_chars'] = min(avail_chars, b.get('max_inject_chars', 15000))
    return b


def fill_budget(prof, cands, want=None):
    """按分数贪心填到预算上限，返回 (选中块, 统计)。遵守 min/max_chunks 与字数上限。"""
    b = budget(prof)
    lo = b.get('min_chunks', 2)
    hi = b.get('max_chunks', 8)
    want = max(lo, min(hi, want or b['default_chunks']))
    cap = b['avail_chars']
    picked, used = [], 0
    for c in cands:
        if len(picked) >= want:
            break
        if used + c['chars'] > cap:
            continue
        picked.append(c)
        used += c['chars']
    if len(picked) < lo:
        for c in cands:
            if len(picked) >= lo or len(picked) >= hi:
                break
            if c in picked:
                continue
            if used + c['chars'] > b.get('hard_ceiling_chars', 24000):
                continue
            picked.append(c)
            used += c['chars']
    return picked, {'chars': used, 'entry_chars': prof['entry_chars'], 'cap': cap,
                    'avail_tokens': b['avail_tokens'], 'want': want}


# ---------------- 入口（全量 / 精简，带缓存） ----------------
_ENTRY_CACHE = {}
SLIM_HINT = ('总则', '角色', 'ROLE', '协议', '输出', '字段', '格式', '自检', '必守', '硬规则',
             '负面', '预算', '写法', '标签', '质量', '镜头', '光影', '服装', '姿势')


def slim_entry(prof, limit=6000):
    """自动挑「最像规则」的块拼一个短入口。规则类块优先，其次画师/台账类。"""
    BAN = ('台账', '成长', '历史', '归档', '评测', '训练')
    out, used = [], 0
    # 1) 强制保留「输出契约」块：正文里同时出现 nh 与 q: 的那种
    for b in prof['blocks']:
        t = (b.get('text') or '')
        if 'nh' in t and ('q:' in t or 'q :' in t or 'q／' in t):
            fp = os.path.join(prof['dir'], b['file'])
            if os.path.exists(fp):
                body = _read(fp).strip()
                out.append(body)
                used += b['chars']
            if used > limit * 0.5:
                break
    pri = []
    for b in prof['blocks']:
        t = b['title'].lower()
        if any(x in t for x in BAN):
            continue
        if any(b['title'] == '' for _ in [0]):
            continue
        s = sum(1 for k in SLIM_HINT if k.lower() in t)
        if s:
            pri.append((s, b))
    pri.sort(key=lambda x: (-x[0], x[1]['chars']))
    out, used = [], 0
    for _, b in pri:
        if used + b['chars'] > limit:
            continue
        p = os.path.join(prof['dir'], b['file'])
        if not os.path.exists(p):
            continue
        out.append(_read(p).strip())
        used += b['chars']
        if used >= limit * 0.9:
            break
    return '\n\n'.join(out)


def _head(prof, limit):
    """按 system.md 的章节边界截头部，保证「角色 + 输出契约」这类开头一定在。"""
    t = prof.get('entry_text') or ''
    if len(t) <= limit:
        return t
    cut = t.rfind('\n## ', 0, limit)
    if cut < limit * 0.6:
        cut = limit
    return t[:cut].rstrip() + '\n\n（以下小节略）'


def entry_for(prof, mode=None):
    mode = mode or prof.get('entry_mode') or 'full'
    key = (prof['dir'], mode)
    if key in _ENTRY_CACHE:
        return _ENTRY_CACHE[key]
    if mode == 'slim':
        txt = _head(prof, 6000)
    elif mode == 'core':
        txt = _head(prof, 3000)
    else:
        txt = prof.get('entry_text') or ''
    _ENTRY_CACHE[key] = txt
    return txt


def expand_query(prof, text, model=None):
    """用本地模型把中文自然语改写成英文 tag，用于提升检索命中。失败则原样返回。"""
    cfg = load_cfg()
    m = model or model_of(cfg)
    sysmsg = ('把用户的描述转成 8-15 个英文 Danbooru 风格 tag，只输出 tag，逗号分隔，'
              '不要数字编号、不要解释、不要句子。优先写：人数、场景、动作、服装、镜头、光线。')
    out, err = _chat(m, sysmsg, text, 0.2, 0.85, 160, True)
    if err or not out:
        return '', err
    tags = ','.join(x.strip() for x in out.replace('\n', ',').split(',') if x.strip())
    return tags[:400], None


# ---------------- 生成 ----------------
def generic_frame(prof):
    """入口为空时的通用格式兜底（只定角色与输出形态，不含内容）。"""
    if prof.get('output_format') == 'fields':
        keys = []
        for f in prof.get('fields', []):
            k = (f.get('key') or f.get('name')) if isinstance(f, dict) else f
            if k:
                keys.append(str(k))
        keys = keys or ['prompt']
        head = '你是提示词工程师。把用户的中文需求直接写成提示词，严格按以下字段逐行输出：'
        return (head + '\n' + ' / '.join(keys) + '\n'
                + '每行格式「字段: 内容」，英文、小写、逗号分隔；字段一个都不许省。\n'
                + '不解释、不评价、不复述需求、不加任何说明，第一行就是第一个字段。')
    return ('你是提示词工程师。把用户的中文需求直接写成可用的提示词正文，分节自然语言，'
            '场景、主体、动作、镜头、质感写清即可。\n'
            '不解释、不评价、不加任何说明，第一行就是正文。')


def build_prompt(prof, picked, user_text, mode=None):
    parts = [entry_for(prof, mode) or generic_frame(prof)]
    if picked:
        ref = ['\n\n## 参考资料（只作依据，不要照抄格式）']
        for i, b in enumerate(picked, 1):
            p = os.path.join(prof['dir'], b['file'])
            body = _read(p) if os.path.exists(p) else ''
            ref.append(f'\n### [{i}] {b["title"]}\n{body.strip()}')
        parts.append('\n'.join(ref))
    sysmsg = '\n'.join(x for x in parts if x)
    return sysmsg, user_text


def generate(prof, user_text, picked, model=None, retry=True, entry_mode=None, temp_bump=0):
    cfg = load_cfg()
    m = model or model_of(cfg)
    g = prof.get('generate', {})
    sysmsg, usr = build_prompt(prof, picked, user_text, entry_mode)
    t0 = time.time()
    base_t = min(1.2, g.get('temperature', 0.75) + (temp_bump or 0))
    out, err = _chat(m, sysmsg, usr, base_t,
                     g.get('top_p', 0.9), g.get('max_output_tokens', 4096),
                     g.get('enable_thinking', False))
    if err:
        return {'ok': False, 'error': err, 'sec': round(time.time() - t0, 1)}
    issues = validate(prof, out)
    retried = False
    fmt = prof.get('output_format', 'auto')
    need_retry = any('空' in i for i in issues) or (fmt in ('fields', 'oneline', 'paragraph') and issues)
    if issues and retry and need_retry:
        retried = True
        out2, err2 = _chat(m, sysmsg, usr + '\n\n（重新输出：第一行必须是第一个字段，不要任何说明）',
                           min(1.2, base_t + 0.1),
                           g.get('top_p', 0.9), g.get('max_output_tokens', 4096),
                           g.get('enable_thinking', False))
        if not err2:
            issues2 = validate(prof, out2)
            if len(issues2) <= len(issues):
                out, issues = out2, issues2
    return {'ok': not issues, 'text': out, 'issues': issues, 'retried': retried,
            'sec': round(time.time() - t0, 1),
            'chars': len(re.sub(r'\s', '', sysmsg)) + len(user_text)}


def _chat(m, system, user, temp, top_p, max_tok, no_think):
    body = {
        'model': m['model'],
        'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
        'temperature': temp, 'top_p': top_p, 'max_tokens': max_tok,
    }
    if no_think:
        body['enable_thinking'] = False
        body['chat_template_kwargs'] = {'enable_thinking': False}
        # Qwen3.8 / Bonsai 系的 chat template 没有 thinking-budget hook，
        # chat_template_kwargs 会被静默忽略，必须发顶层字段才真关掉思考：
        #   Ollama           → think: false
        #   llama.cpp / 官方 → reasoning_effort: "none"
        host = (m.get('base_url') or '').lower()
        if ':11434' in host or 'ollama' in host:
            body['think'] = False
        else:
            body['reasoning_effort'] = 'none'
    url = m['base_url'].rstrip('/') + '/chat/completions'
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json',
                                          'Authorization': 'Bearer ' + m.get('api_key', 'sk-local')})
    try:
        with urllib.request.urlopen(req, timeout=m.get('timeout_sec', 300)) as r:
            d = json.loads(r.read().decode())
        return d['choices'][0]['message']['content'], None
    except urllib.error.URLError as e:
        return None, f'连不上模型服务（{m["base_url"]}）：{e}'
    except Exception as e:
        return None, f'生成失败：{e}'


# ---------------- 校验 ----------------
FIELD_RE = re.compile(r'^\s*\**([a-z]{1,3})\**\s*[:：]\s*(.*)$', re.M)


def parse_fields(text):
    got, order = {}, []
    for ln in text.strip().splitlines():
        m = FIELD_RE.match(ln.strip())
        if m:
            k = m.group(1)
            got[k] = m.group(2).strip()
            order.append(k)
    return got, order


def validate(prof, text):
    issues = []
    v = prof.get('validate', {})
    fmt = prof.get('output_format', 'fields')
    if not text or not text.strip():
        return ['输出为空']
    if fmt == 'fields':
        raw = prof.get('fields', [])
        fields = []
        for f in raw:
            k = (f.get('key') or f.get('name')) if isinstance(f, dict) else f
            if k:
                fields.append(str(k))
        if not fields:
            fields = ['q']
        got, order = parse_fields(text)
        miss = [f for f in fields if f not in got]
        if miss:
            issues.append('缺字段：' + ','.join(miss))
        ms = v.get('must_start_with')
        if ms and order and order[0] != ms:
            issues.append(f'第一行应为 {ms}:')
        low = text.lower()
        for w in v.get('ban_light_words', []):
            if w.lower() in low:
                issues.append('编造光词：' + w)
    else:
        low = text.lower()
        rx = v.get('regex')
        if rx:
            try:
                if not re.search(rx, text, re.M):
                    issues.append('不匹配格式：' + rx)
            except re.error:
                pass
        keys = v.get('require_keys') or []
        if keys:
            body = re.sub(r'^```[a-zA-Z]*\n|```$', '', text.strip())
            try:
                obj = json.loads(body)
                miss_k = [k for k in keys if k not in obj]
                if miss_k:
                    issues.append('JSON 缺键：' + ','.join(miss_k))
            except Exception:
                issues.append('不是合法 JSON')
        for w in v.get('ban_light_words', []):
            if w.lower() in low:
                issues.append('编造光词：' + w)
        n = len(re.sub(r'\s', '', text))
        if n < v.get('min_chars', 0):
            issues.append(f'过短（{n} 字）')
        if v.get('max_chars') and n > v['max_chars']:
            issues.append(f'过长（{n} 字）')
    return issues


# ---------------- CLI ----------------
if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    cfg = load_cfg()
    rs = discover(cfg)
    print(f'规则集 {len(rs)} 套')
    for p in rs:
        try:
            rel = os.path.relpath(p['dir'], ROOT)
        except ValueError:
            rel = p['dir']
        print(f'  · {p["id"]:<18} 块 {len(p["blocks"]):>4}  正文 {p["entry_chars"]:>6} 字  {rel}')
    if len(sys.argv) > 2:
        rid, q = sys.argv[1], sys.argv[2]
        p = next(x for x in rs if x['id'] == rid)
        cands = retrieve(p, q)
        picked, st = fill_budget(p, cands)
        print(f'\n查询：{q}')
        for c in cands[:8]:
            print(f'  {c["score"]:.2f}  {c["title"][:40]}  {c["chars"]}字  命中{c["why"]}')
        print(f'选中 {len(picked)} 块 / {st["chars"]} 字 · 预算上限 {st["cap"]} 字')
