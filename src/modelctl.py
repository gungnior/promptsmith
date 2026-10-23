#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地模型进程管理：启动 / 停止 / 状态查询。零依赖。

设计要点：
- 只管理 config.json 里 model_profiles[].launch 声明的进程
- 优先管自己拉起的子进程（owned）；用户手动启动的也能停（按端口反查 PID + 校验进程名）
- 就绪判定不靠 stdout，靠轮询 HTTP（llama.cpp: /health，Ollama: /api/tags）
- 停止支持三档：terminate（优雅）→ 等待 → kill（强制）
"""
import json, os, re, shutil, subprocess, sys, time, urllib.error, urllib.request

IS_WIN = sys.platform.startswith('win')
_OWNED = {}      # model_id -> Popen
_LOG = {}        # model_id -> [最近 N 行 stdout/stderr]


def _no_window():
    if IS_WIN:
        return {'creationflags': 0x08000000}   # CREATE_NO_WINDOW，不弹黑框
    return {}


def _run(cmd, timeout=10):
    """跑命令并解码输出。Windows 中文系统的 netstat/tasklist 输出是 GBK，不能假定 UTF-8。"""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, **_no_window())
        raw = p.stdout or b''
        for enc in ('utf-8', 'gbk', 'latin-1'):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode('latin-1', 'ignore')
    except Exception:
        return ''


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 本地服务不走代理


def _http_ok(url, timeout=2.5):
    """本地端口是否可访问。只用空代理 opener：系统代理会把 127.0.0.1 也劫持，
    那样代理的 502 会被误判成"服务活着"。"""
    try:
        with _OPENER.open(url, timeout=timeout) as r:
            return 200 <= r.status < 400
    except urllib.error.HTTPError as e:
        return e.code in (401, 403)      # 服务在线但要求鉴权
    except Exception:
        return False


def health_ok(base_url, ready_path=None):
    """模型服务是否已就绪。依次试用户指定路径、llama.cpp /health、OpenAI /models。"""
    base = (base_url or '').rstrip('/')
    if not base:
        return False
    paths = []
    if ready_path:
        paths.append(ready_path)
    paths += ['/health', '/v1/models', '/api/tags']
    for p in paths:
        if _http_ok(base + p):
            return True
    return False


def port_pid(port):
    """按监听端口反查 PID（Windows 用 netstat，类 Unix 用 lsof）。找不到返回 None。"""
    if IS_WIN:
        for ln in _run(['netstat', '-ano']).splitlines():
            if f':{port}' in ln and 'LISTENING' in ln.upper():
                parts = ln.split()
                if parts and parts[-1].isdigit():
                    return int(parts[-1])
    else:
        out = _run(['lsof', '-nP', f'-iTCP:{port}', '-sTCP:LISTEN', '-t']).strip()
        if out:
            return int(out.splitlines()[0])
    return None


def pid_alive(pid):
    if not pid:
        return False
    try:
        if IS_WIN:
            return str(pid) in _run(['tasklist', '/FI', f'PID eq {pid}', '/NH'])
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def proc_name(pid):
    if IS_WIN:
        out = _run(['tasklist', '/FI', f'PID eq {pid}', '/NH', '/FO', 'CSV']).strip()
        m = re.match(r'^"([^"]+)"', out)
        return (m.group(1) if m else '').lower()
    return _run(['ps', '-p', str(pid), '-o', 'comm=']).strip().lower()


def gpu_used_mb():
    if not shutil.which('nvidia-smi'):
        return None
    out = _run(['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'], timeout=8)
    vals = [int(x) for x in re.findall(r'\d+', out)]
    return max(vals) if vals else None


def _tail(model_id, n=6):
    return (_LOG.get(model_id) or [])[-n:]


def status(model):
    """返回模型当前状态。model = model_profiles 里的一项。"""
    launch = model.get('launch') or {}
    base = model.get('base_url') or ''
    port = launch.get('port') or _port_of(base)
    ready = health_ok(base, launch.get('ready_path'))
    pid = None
    owned = False
    proc = _OWNED.get(model.get('id'))
    if proc is not None:
        if proc.poll() is None:
            pid, owned = proc.pid, True
        else:
            _OWNED.pop(model.get('id'), None)
    if pid is None and port:
        pid = port_pid(port)
    running = bool(pid) or ready
    return {
        'id': model.get('id'),
        'model': model.get('model'),
        'base_url': base,
        'port': port,
        'running': running,
        'ready': ready,
        'pid': pid,
        'owned': owned,
        'launch_enabled': bool(launch.get('enabled')),
        'auto_start': bool(launch.get('auto_start')),
        'gpu_used_mb': gpu_used_mb(),
        'log': _tail(model.get('id')),
    }


def _port_of(base_url):
    m = re.search(r':(\d{2,5})', base_url or '')
    return int(m.group(1)) if m else None


def start(model, timeout=None, wait=True):
    """启动模型服务。返回 (ok, msg, status)。"""
    launch = model.get('launch') or {}
    mid = model.get('id')
    if not launch.get('enabled'):
        return False, '该模型未配置启动命令（model_profiles[].launch.enabled）', status(model)
    cmd = launch.get('command')
    if not cmd or not os.path.exists(cmd):
        return False, f'启动程序不存在：{cmd}', status(model)
    st = status(model)
    if st['ready']:
        return True, '已在运行', st
    if st['running'] and not st['ready']:
        return False, '进程存在但还没就绪（可能正在加载模型）', st

    args = [cmd] + [str(a) for a in (launch.get('args') or [])]
    cwd = launch.get('cwd') or os.path.dirname(cmd) or None
    work = os.path.dirname(os.path.abspath(cmd))
    try:
        logf = open(os.path.join(_log_dir(), f"{mid or 'model'}.log"), 'a', encoding='utf-8', errors='ignore')
        logf.write(f'\n===== {time.strftime("%Y-%m-%d %H:%M:%S")} start =====\n')
        logf.flush()
        p = subprocess.Popen(args, cwd=cwd, stdout=logf, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, **_no_window())
        _OWNED[mid] = p
        _LOG[mid] = []
    except Exception as e:
        return False, f'启动失败：{e}', status(model)

    to = timeout or int(launch.get('startup_timeout_sec') or 150)
    if not wait:
        return True, '已发起启动', status(model)
    t0 = time.time()
    while time.time() - t0 < to:
        if p.poll() is not None:
            return False, f'进程已退出（code={p.returncode}），看日志', status(model)
        if health_ok(model.get('base_url') or '', launch.get('ready_path')):
            st = status(model)
            return True, f'已就绪（{time.time() - t0:.0f}s）', st
        time.sleep(1.5)
    return False, f'启动超时（{to}s），进程仍在但服务未就绪', status(model)


def stop(model, timeout=15):
    """停止模型服务。自己拉起的直接 terminate；外部启动的按端口反查 PID 后校验进程名再杀。"""
    mid = model.get('id')
    launch = model.get('launch') or {}
    st = status(model)
    if not st['running']:
        return True, '本来就没在跑', st

    # 1) 自己拉起的子进程
    p = _OWNED.get(mid)
    if p is not None and p.poll() is None:
        try:
            p.terminate()
            for _ in range(timeout * 2):
                if p.poll() is not None:
                    _OWNED.pop(mid, None)
                    return True, '已停止', status(model)
                time.sleep(0.5)
            p.kill()
            _OWNED.pop(mid, None)
            return True, '已强制停止', status(model)
        except Exception as e:
            return False, f'停止失败：{e}', status(model)

    # 2) 外部进程：按端口找 PID，校验进程名与启动程序同名才杀
    pid = st['pid']
    if not pid:
        return False, '找不到进程（可能已退出）', st
    want = os.path.splitext(os.path.basename(launch.get('command') or ''))[0].lower()
    got = proc_name(pid)
    if want and got and want not in got:
        return False, f'端口 {st["port"]} 的进程是 {got}，不是 {want}，已放弃（避免误杀）', st
    try:
        if IS_WIN:
            _run(['taskkill', '/PID', str(pid), '/T', '/F'], timeout=15)
        else:
            os.kill(pid, 15)
            time.sleep(2)
            if pid_alive(pid):
                os.kill(pid, 9)
        time.sleep(1.5)
        return True, '已停止（外部进程）', status(model)
    except Exception as e:
        return False, f'停止失败：{e}', status(model)


def _log_dir():
    d = os.path.join(os.environ.get('APPDATA') or os.path.expanduser('~'), 'PromptSmith', 'logs')
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = os.path.abspath('.')
    return d


def stop_all(models):
    out = []
    for m in models:
        if (m.get('launch') or {}).get('enabled'):
            ok, msg, _ = stop(m)
            out.append((m.get('id'), ok, msg))
    return out
