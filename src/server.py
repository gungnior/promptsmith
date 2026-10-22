#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PromptSmith HTTP 服务：只监听 127.0.0.1，零第三方依赖。"""
import json, os, sys, threading, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import promptsmith as PS

ROOT = PS.ROOT
WEB = os.path.join(ROOT, 'web')
_STATE = {'rulesets': [], 'ts': 0}


def refresh():
    cfg = PS.load_cfg()
    _STATE['rulesets'] = PS.discover(cfg)
    _STATE['ts'] = 1
    return _STATE['rulesets']


def get_rs(rid):
    rs = _STATE['rulesets'] or refresh()
    for p in rs:
        if p['id'] == rid:
            return p
    return rs[0] if rs else None


class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        b = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(b)

    # ---------- GET ----------
    def do_GET(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        qs = parse_qs(u.query)
        if path in ('/', '/index.html'):
            p = os.path.join(WEB, 'ui.html')
            if not os.path.exists(p):
                return self._send(404, 'ui.html 不存在')
            return self._send(200, open(p, 'rb').read(), 'text/html; charset=utf-8')

        if path == '/api/bootstrap':
            cfg = PS.load_cfg()
            rs = _STATE['rulesets'] or refresh()
            cur = get_rs(cfg.get('active_ruleset') or (rs[0]['id'] if rs else None))
            return self._send(200, {
                'rulesets': [{'id': p['id'], 'display_name': p.get('display_name') or p['id'],
                              'blocks': len(p['blocks']), 'entry_chars': len(PS.entry_for(p, None)),
                              'entry_mode': p.get('entry_mode', 'full'),
                              'target_model': p.get('target_model', ''), 'kind': p.get('target_kind', ''),
                              } for p in rs],
                'active': cur['id'] if cur else None,
                'model': {k: v for k, v in PS.model_of(cfg).items() if k != 'api_key'},
                'blocks': [{'id': b['id'], 't': b['title'], 'c': b['chars'],
                            'kw': b.get('keywords', [])[:6]} for b in (cur['blocks'] if cur else [])],
                'budget': PS.budget(cur) if cur else {},
                'fields': cur.get('fields', []) if cur else [],
                'vector': PS.vector_status(cur) if cur else {},
            })

        if path == '/api/retrieve':
            rid = (qs.get('rs') or [''])[0]
            q = (qs.get('q') or [''])[0]
            p = get_rs(rid)
            if not p:
                return self._send(400, {'error': '没有可用的规则集'})
            cands = PS.retrieve(p, q)
            picked, st = PS.fill_budget(p, cands)
            return self._send(200, {'candidates': cands[:40], 'picked': [c['id'] for c in picked], 'stat': st})

        if path == '/api/config':
            cfg = PS.load_cfg()
            port = self.server.server_address[1]
            return self._send(200, {'models': cfg.get('model_profiles', []),
                                    'active': cfg.get('active_model'),
                                    'budget': cfg.get('budget', {}),
                                    'vector': cfg.get('vector', {}),
                                    'port': port,
                                    'ruleset_roots': cfg.get('ruleset_roots', [])})

        if path == '/api/fav':
            return self._send(200, {'items': PS.fav_list()})

        if path == '/api/vector':
            p = get_rs((qs.get('rs') or [''])[0])
            if not p:
                return self._send(400, {'error': '没有可用的规则集'})
            return self._send(200, PS.vector_status(p))

        if path == '/api/heat':
            return self._send(200, {'dict': PS.tagdict_stat()})

        if path == '/api/dict':
            d = PS.zh2tag()
            mp = os.path.join(ROOT, 'data', 'misses.json')
            miss = []
            if os.path.exists(mp):
                try:
                    miss = json.load(open(mp, encoding='utf-8'))
                except Exception:
                    miss = []
            return self._send(200, {'dict': d, 'misses': miss})

        if path == '/api/search':
            rid = (qs.get('rs') or [''])[0]
            q = (qs.get('q') or [''])[0]
            p = get_rs(rid)
            if not p:
                return self._send(400, {'error': '没有可用的规则集'})
            return self._send(200, {'hits': PS.search_content(p, q)})

        if path == '/api/block':
            rid, bid = (qs.get('rs') or [''])[0], (qs.get('id') or [''])[0]
            p = get_rs(rid)
            for b in (p['blocks'] if p else []):
                if b['id'] == bid:
                    fp = os.path.join(p['dir'], b['file'])
                    txt = open(fp, encoding='utf-8', errors='ignore').read() if os.path.exists(fp) else ''
                    return self._send(200, {'id': bid, 'title': b['title'], 'chars': b['chars'], 'text': txt})
            return self._send(404, {'error': '块不存在'})

        return self._send(404, {'error': 'not found'})

    # ---------- POST ----------
    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get('Content-Length') or 0)
        try:
            data = json.loads(self.rfile.read(n) or b'{}')
        except Exception:
            return self._send(400, {'error': 'bad json'})

        if u.path == '/api/generate':
            p = get_rs(data.get('rs'))
            if not p:
                return self._send(400, {'error': '没有可用的规则集'})
            text = (data.get('text') or '').strip()
            if not text:
                return self._send(400, {'error': '输入为空'})
            ids = data.get('blocks') or []
            locked_ids = data.get('locked') or []
            over = None
            expanded = ''
            if data.get('expand'):
                expanded, ex_err = PS.expand_query(p, text)
                if expanded:
                    text = text + ' ' + expanded
            cands = PS.retrieve(p, text)
            bymap = {c['id']: c for c in cands}
            # 用户手动指定的块：从全量里补
            if ids:
                allmap = {b['id']: b for b in p['blocks']}
                picked = []
                for i in ids:
                    c = bymap.get(i) or allmap.get(i)
                    if c:
                        picked.append({'score': 0, 'why': ['手动'],
                                       **{k: v for k, v in c.items() if k not in ('t', 'c')}})
                if not picked:
                    if not cands:
                        cands = PS.retrieve(p, text)
                    picked, _ = PS.fill_budget(p, cands)
                    if not picked:
                        PS.record_miss(text)
                hc = p['budget'].get('hard_ceiling_chars', 24000)
                cap = p['budget'].get('max_inject_chars', 15000)
                tot = sum(x['chars'] for x in picked)
                if tot > hc:
                    sug = sorted([{'id': x['id'], 'title': x['title'], 'chars': x['chars']} for x in picked],
                                 key=lambda y: y['chars'], reverse=True)[:3]
                    return self._send(200, {
                        'ok': False,
                        'error': f'超出硬顶：{tot} / {hc} 字，请删掉至少一块再生成',
                        'over': {'total': tot, 'cap': hc, 'hard': True, 'suggest': sug}})
                over = {'total': tot, 'cap': cap, 'hard': False,
                        'suggest': [{'id': x['id'], 'title': x['title'], 'chars': x['chars']}
                                    for x in picked if x['chars'] > 800][:3]} if tot > cap else None
            else:
                picked, _ = PS.fill_budget(p, cands)
                if not picked:
                    hits = PS.search_content(p, text, limit=12)
                    cap = p['budget'].get('max_inject_chars', 15000)
                    tot = 0
                    for h in hits:
                        if tot + h['c'] > cap:
                            continue
                        picked.append({'score': 0, 'why': ['正文兜底'], 'id': h['id'],
                                       'title': h['t'], 'file': next((b['file'] for b in p['blocks']
                                                                     if b['id'] == h['id']), ''),
                                       'chars': h['c']})
                        tot += h['c']
                        if len(picked) >= p['budget'].get('default_chunks', 6):
                            break
                if not picked:
                    PS.record_miss(text)
                if locked_ids:
                    allmap = {b['id']: b for b in p['blocks']}
                    have = {x['id'] for x in picked}
                    forced = []
                    for i in locked_ids:
                        if i in have or i not in allmap:
                            continue
                        b = allmap[i]
                        forced.append({'score': 0, 'why': ['锁定'], 'id': b['id'], 'title': b['title'],
                                       'file': b['file'], 'chars': b['chars']})
                    picked = forced + picked
            if ids:
                for i in ids[:8]:
                    try:
                        PS.learn_alias(p, text, i)
                    except Exception:
                        pass
            r = PS.generate(p, text, picked, retry=bool(data.get('retry', True)),
                            entry_mode=data.get('entry_mode'), temp_bump=float(data.get('bump') or 0))
            r['used'] = [{'id': b['id'], 'title': b['title'], 'chars': b['chars'],
                          'why': b.get('why') or []} for b in picked]
            r['over'] = over
            r['expanded'] = expanded
            print(f"[gen] rs={p['id']} ok={r.get('ok')} sec={r.get('sec')} "
                  f"issues={r.get('issues')} err={str(r.get('error'))[:80]} blocks={len(picked)}", flush=True)
            return self._send(200, r)

        if u.path == '/api/ruleset':
            # 保存常驻块 / 切换
            rid = data.get('rs')
            p = get_rs(rid)
            if not p:
                return self._send(400, {'error': 'no ruleset'})
            pf = os.path.join(p['dir'], 'profile.json')
            prof = json.load(open(pf, encoding='utf-8')) if os.path.exists(pf) else {}
            if 'resident_blocks' in data:
                prof['resident_blocks'] = data['resident_blocks']
            if 'entry_mode' in data:
                prof['entry_mode'] = data['entry_mode']
            if 'budget' in data:
                prof.setdefault('budget', {}).update(data['budget'])
            if 'select' in data:
                prof.setdefault('select', {}).update(data['select'])
            json.dump(prof, open(pf, 'w', encoding='utf-8', newline='\n'), ensure_ascii=False, indent=1)
            refresh()
            return self._send(200, {'ok': True, 'entry_chars': get_rs(rid)['entry_chars']})

        if u.path == '/api/active':
            cfg = PS.load_cfg()
            cfg['active_ruleset'] = data.get('rs')
            PS.save_cfg(cfg)
            return self._send(200, {'ok': True})

        if u.path == '/api/config':
            cfg = PS.load_cfg()
            if isinstance(data.get('models'), list) and data['models']:
                cfg['model_profiles'] = data['models']
            if data.get('active'):
                cfg['active_model'] = data['active']
            if isinstance(data.get('budget'), dict):
                cfg.setdefault('budget', {}).update(data['budget'])
            if isinstance(data.get('ruleset_roots'), list):
                cfg['ruleset_roots'] = data['ruleset_roots']
            if isinstance(data.get('vector'), dict):
                cfg.setdefault('vector', {}).update(data['vector'])
            if data.get('port'):
                cfg['server_port'] = int(data['port'])
            PS.save_cfg(cfg)
            refresh()
            return self._send(200, {'ok': True})

        if u.path == '/api/block':
            p = get_rs(data.get('rs'))
            if not p:
                return self._send(400, {'error': 'no ruleset'})
            bid = data.get('id')
            if data.get('restore'):
                ok, msg = PS.restore_block(p, bid)
            else:
                ok, msg = PS.write_block(p, bid, data.get('text') or '')
            if ok:
                refresh()
            return self._send(200, {'ok': ok, 'msg': msg, 'path': msg if ok else ''})

        if u.path == '/api/ruleset/add':
            ok, msg = PS.add_ruleset_root(data.get('path') or '')
            if ok:
                refresh()
            return self._send(200, {'ok': ok, 'msg': msg, 'rulesets': [x['id'] for x in _STATE['rulesets']]})

        if u.path == '/api/fav':
            if data.get('del'):
                return self._send(200, {'ok': PS.fav_del(data['del']), 'items': PS.fav_list()})
            ok, msg = PS.fav_add(data.get('add') or data)
            return self._send(200, {'ok': ok, 'msg': msg, 'items': PS.fav_list()})

        if u.path == '/api/vector':
            p = get_rs(data.get('rs'))
            if not p:
                return self._send(400, {'error': '没有可用的规则集'})
            act = data.get('action') or 'build'
            if act in ('enable', 'disable'):
                cfg = PS.load_cfg()
                cfg.setdefault('vector', {})['enabled'] = (act == 'enable')
                PS.save_cfg(cfg)
                return self._send(200, {'ok': True, 'status': PS.vector_status(p)})
            if act == 'build':
                st = PS.vector_status(p)
                if (st.get('progress') or {}).get('state') == 'running':
                    return self._send(200, {'ok': True, 'status': st, 'note': '已在构建中'})
                threading.Thread(target=PS.build_vectors, args=(p, bool(data.get('force'))),
                                 daemon=True).start()
                return self._send(200, {'ok': True, 'status': PS.vector_status(p)})
            return self._send(400, {'error': 'unknown action'})

        if u.path == '/api/heat':
            tags = data.get('tags')
            if not tags:
                # 默认抓全局词表的英文 tag 值 + 检索时在块里出现过的英文词
                tags = sorted({v for v in PS.zh2tag().values() if isinstance(v, str)})
                p = get_rs(data.get('rs'))
                if p:
                    seen = set()
                    for b in p['blocks'][:600]:
                        for w in PS.TOK.findall((b.get('text') or '')[:3000]):
                            if len(w) > 4:
                                seen.add(w.lower())
                    tags = sorted(set(tags) | seen)[:400]
            tags = [t for t in tags if isinstance(t, str)][:400]
            PS._TAG_PROG.update({'state': 'running', 'done': 0, 'total': len(tags),
                                 'msg': f'开始抓 {len(tags)} 个 tag'})
            threading.Thread(target=PS.fetch_taginfo, args=(tags, PS._TAG_PROG),
                             daemon=True).start()
            return self._send(200, {'ok': True, 'queued': len(tags)})

        if u.path == '/api/dict':
            d = PS.zh2tag()
            if data.get('del'):
                d.pop(data['del'], None)
            for k, v in (data.get('add') or {}).items():
                if k and v:
                    d[k] = v
            PS.save_zh2tag(d)
            return self._send(200, {'ok': True, 'n': len(d)})

        if u.path == '/api/reload':
            refresh()
            return self._send(200, {'ok': True, 'rulesets': len(_STATE['rulesets'])})

        return self._send(404, {'error': 'not found'})


def serve(port=8765, open_browser=True):
    for p in range(port, port + 12):
        try:
            httpd = ThreadingHTTPServer(('127.0.0.1', p), H)
            break
        except OSError:
            continue
    else:
        raise SystemExit('8765-8776 全部被占用')
    url = f'http://127.0.0.1:{p}/'
    print(f'PromptSmith 已启动：{url}')
    rs = refresh()
    for x in rs:
        print(f'  · {x["id"]:<16} {len(x["blocks"]):>4} 块  正文 {x["entry_chars"]} 字')
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(PS.load_cfg().get('server_port') or 8765)
    serve(port, open_browser='--no-browser' not in sys.argv)
