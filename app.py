"""
Enjin Visual Novel & Sokongan Root PFS (Auto-Launch)
---------------------------------------------------------
"""
import os
import re
import hmac
import secrets
import json
import shutil
import threading
import time
import zipfile
from pathlib import Path

import requests

try:  
    import py7zr
except ImportError:
    py7zr = None
from urllib.parse import quote

from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory

BASE = Path(__file__).resolve().parent
CACHE = Path(os.environ.get("CACHE_DIR", BASE / "cache")).resolve()
LOCAL_ZIPS = BASE / "zips"  
GH_REPO = os.environ.get("GITHUB_REPO", "").strip()
GH_PATH = os.environ.get("GITHUB_GAMES_PATH", "games").strip("/ ")
GH_BRANCH = os.environ.get("GITHUB_BRANCH", "").strip()
GH_RELEASES = os.environ.get("GITHUB_USE_RELEASES", "1") != "0"
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
ADMIN = os.environ.get("ADMIN_PASSWORD", "")
MAX_DL = int(os.environ.get("MAX_DOWNLOAD_MB", "6144")) * 1024 * 1024
MAX_UNZIP = int(os.environ.get("MAX_UNZIPPED_MB", "12288")) * 1024 * 1024
GH_TAG = os.environ.get("GITHUB_RELEASE_TAG", "vn-games").strip() or "vn-games"
GH_WRITE = bool(GH_REPO and GH_TOKEN)   
PART = 1900 * 1024 * 1024               
CHUNK = 16 * 1024 * 1024                
PENDING = CACHE / ".pending"
LIST_TTL = 45  

CACHE.mkdir(parents=True, exist_ok=True)
shutil.rmtree(CACHE / ".pending", ignore_errors=True)
(CACHE / ".pending").mkdir(exist_ok=True)
for _t in CACHE.glob(".*.tmp"):
    shutil.rmtree(_t, ignore_errors=True)

SAFE = re.compile(r"^[\w\-]+$")
POS = {"kiri": "left", "tengah": "center", "kanan": "right",
       "left": "left", "center": "center", "right": "right"}


# ------------------------------------------------------------------ skrip .vn
def num(v):
    try:
        return float(v) if "." in v else int(v)
    except ValueError:
        return v


def parse_script(text):
    cmds, labels = [], {}
    pending = None
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if line.startswith("?"):
            m = re.match(r"\?\s*(.+?)\s*->\s*(\S+)$", line)
            if not m:
                raise ValueError(f"Baris {n}: pilihan tidak sah: {line}")
            if pending is None:
                pending = {"t": "choice", "options": []}
                cmds.append(pending)
            pending["options"].append({"text": m.group(1), "label": m.group(2)})
            continue
        pending = None
        if line.startswith("@"):
            p = line[1:].split()
            c, a = (p[0].lower() if p else ""), p[1:]
            if c == "label" and a:
                labels[a[0]] = len(cmds)
            elif c == "bg" and a:
                cmds.append({"t": "bg", "v": a[0], "fade": "fade" in a[1:]})
            elif c == "show" and a:
                cmds.append({"t": "show", "name": a[0],
                             "pose": a[1] if len(a) > 1 else "biasa",
                             "pos": POS.get(a[2], "center") if len(a) > 2 else "center"})
            elif c == "hide" and a:
                cmds.append({"t": "hide", "name": a[0]})
            elif c in ("bgm", "se", "voice") and a:
                cmds.append({"t": c, "v": a[0]})
            elif c == "set":
                m = re.match(r"@set\s+(\w+)\s*(\+=|=)\s*(.+)$", line)
                if not m:
                    raise ValueError(f"Baris {n}: @set tidak sah")
                cmds.append({"t": "set", "k": m.group(1), "op": m.group(2),
                             "v": num(m.group(3).strip())})
            elif c == "if":
                m = re.match(r"@if\s+(\w+)\s*(==|!=|>=|<=|>|<)\s*(\S+)\s*->\s*(\S+)$", line)
                if not m:
                    raise ValueError(f"Baris {n}: @if tidak sah")
                cmds.append({"t": "if", "k": m.group(1), "op": m.group(2),
                             "v": num(m.group(3)), "label": m.group(4)})
            elif c == "jump" and a:
                cmds.append({"t": "jump", "label": a[0]})
            elif c == "end":
                cmds.append({"t": "end"})
            else:
                raise ValueError(f"Baris {n}: arahan tidak dikenali: {line}")
            continue
        m = re.match(r"^([^:]{1,24}):\s+(.+)$", line)
        if m:
            cmds.append({"t": "say", "who": m.group(1).strip(), "text": m.group(2)})
        else:
            cmds.append({"t": "say", "who": "", "text": line})
    for c in cmds:
        for lb in ([c.get("label")] + [o["label"] for o in c.get("options", [])]):
            if lb and lb not in labels:
                raise ValueError(f"Label tidak wujud: {lb}")
    return {"cmds": cmds, "labels": labels}


# ------------------------------------------------------------ struktur folder
def scripts_dir(root):
    return root / "scripts" if (root / "scripts").is_dir() else root


def assets_dir(root):
    return root / "assets" if (root / "assets").is_dir() else root


def list_scripts(root):
    return sorted(p.stem for p in scripts_dir(root).glob("*.vn") if SAFE.match(p.stem))


def find_root(base, max_depth=3):
    """Cari folder game yang mengandungi .vn ATAU fail root.pfs."""
    base = Path(base)
    if list_scripts(base) or (base / "root.pfs").exists() or list(base.glob("root.pfs*")):
        return base
    for dirpath, dirnames, _ in os.walk(base):
        depth = len(Path(dirpath).relative_to(base).parts)
        if depth >= max_depth:
            dirnames[:] = []
            continue
        dirnames.sort()
        for d in dirnames:
            cand = Path(dirpath) / d
            if d != "__MACOSX" and (list_scripts(cand) or (cand / "root.pfs").exists() or list(cand.glob("root.pfs*"))):
                return cand
    return base


# ------------------------------------------------------------- sumber GitHub
def slug(name):
    stem = re.sub(r"\.(zip|7z)$", "", name, flags=re.I)
    return re.sub(r"[^\w\-]+", "-", stem).strip("-").lower() or "game"


def gh_headers(accept="application/vnd.github+json"):
    h = {"Accept": accept, "User-Agent": "vn-engine", "X-GitHub-Api-Version": "2022-11-28"}
    if GH_TOKEN:
        h["Authorization"] = f"Bearer {GH_TOKEN}"
    return h


def gh_get(url, params=None):
    r = requests.get(url, headers=gh_headers(), params=params, timeout=20)
    if r.status_code == 403 and "rate limit" in r.text.lower():
        raise RuntimeError("Had kadar API GitHub dicapai.")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def fetch_sources():
    items, errors = {}, []
    def add(name, size, source, parts=None, **kw):
        if not re.search(r"\.(zip|7z)$", name, re.I):
            return
        gid = slug(name)
        if gid not in items:
            items[gid] = {"id": gid, "name": re.sub(r"\.(zip|7z)$", "", name, flags=re.I),
                          "size": size, "source": source, "parts": parts or [], **kw}

    def asset_part(a):
        return {"url": a["url"] if GH_TOKEN else a["browser_download_url"],
                "accept": "application/octet-stream" if GH_TOKEN else "*/*",
                "size": a["size"], "asset_id": a["id"], "fname": a["name"]}

    if GH_REPO:
        if GH_RELEASES:
            try:
                for rel in gh_get(f"https://api.github.com/repos/{GH_REPO}/releases", {"per_page": 20}) or []:
                    assets = rel.get("assets", [])
                    for a in assets:
                        add(a["name"], a["size"], "release", parts=[asset_part(a)])
            except Exception as e:
                errors.append(f"Release: {e}")
        if GH_PATH:
            try:
                params = {"ref": GH_BRANCH} if GH_BRANCH else None
                data = gh_get(f"https://api.github.com/repos/{GH_REPO}/contents/{GH_PATH}", params)
                for f in data or []:
                    if f.get("type") == "file":
                        part = {"url": f["download_url"] or f["url"], "accept": "*/*", "size": f["size"], "fname": f["name"]}
                        add(f["name"], f["size"], "github", parts=[part])
            except Exception as e:
                errors.append(f"Repo: {e}")
    if LOCAL_ZIPS.is_dir():
        for z in sorted(LOCAL_ZIPS.iterdir()):
            add(z.name, z.stat().st_size, "local", path=str(z))
    return items, "; ".join(errors)


REMOTE = {"t": 0.0, "items": {}, "err": ""}
REMOTE_LOCK = threading.Lock()


def remote(force=False):
    with REMOTE_LOCK:
        if force or time.time() - REMOTE["t"] > LIST_TTL:
            items, err = fetch_sources()
            REMOTE.update(t=time.time(), items=items, err=err)
        return REMOTE["items"], REMOTE["err"]


# ----------------------------------------------------- cache & muat turun
JOBS = {}  
JOBS_LOCK = threading.Lock()


def cached_meta(gid):
    try:
        return json.loads((CACHE / gid / "meta.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def game_root(gid):
    if not SAFE.match(gid):
        abort(400)
    m = cached_meta(gid)
    if not m:
        abort(404)
    return CACHE / gid / "files" / m["root"]


def sniff(path):
    with open(path, "rb") as f:
        h = f.read(6)
    if h[:2] == b"PK":
        return "zip"
    if h == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    return None


def bad_path(out_r, name):
    t = (out_r / name).resolve()
    return Path(name).is_absolute() or (t != out_r and out_r not in t.parents)


def safe_extract(apath, out):
    kind = sniff(apath)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    out_r = out.resolve()
    if kind == "zip":
        with zipfile.ZipFile(apath) as z:
            infos = [i for i in z.infolist() if not i.filename.startswith("__MACOSX")]
            total = sum(i.file_size for i in infos)
            if total > MAX_UNZIP:
                raise ValueError("Saiz selepas ekstrak melebihi had.")
            for i in infos:
                if bad_path(out_r, i.filename):
                    raise ValueError("Laluan tidak selamat.")
            for i in infos:
                z.extract(i, out)
        return total
    if kind == "7z":
        if py7zr is None:
            raise ValueError("Sokongan .7z tidak dipasang.")
        with py7zr.SevenZipFile(apath, "r") as a:
            files = a.files
            total = sum(getattr(f, "uncompressed", 0) or 0 for f in files)
            if total > MAX_UNZIP:
                raise ValueError("Saiz selepas ekstrak melebihi had.")
            a.extractall(path=out)
        return total
    raise ValueError("Fail bukan arkib sah.")


def archive_entries(apath):
    kind = sniff(apath)
    if kind == "zip":
        with zipfile.ZipFile(apath) as z:
            return [(i.filename, i.file_size) for i in z.infolist()]
    if kind == "7z" and py7zr is not None:
        with py7zr.SevenZipFile(apath, "r") as a:
            return [(f.filename, getattr(f, "uncompressed", 0) or 0) for f in a.files]
    return None


def finalize(gid, job, tmp, apath, name, source, delete_archive=True):
    """Terima fail zip/7z yang mengandungi .vn ATAU root.pfs tanpa sekatan."""
    if not sniff(apath):
        raise ValueError("Fail bukan arkib sah.")
    job["state"] = "extracting"
    ents = archive_entries(apath)
    
    has_vn = ents is not None and any(n.lower().endswith(".vn") for n, _ in ents)
    has_pfs = ents is not None and any("root.pfs" in n.lower() for n, _ in ents)
    
    if ents is not None and not has_vn and not has_pfs:
        raise ValueError("Arkib ditolak: tiada fail .vn atau root.pfs dijumpai.")
        
    size = safe_extract(apath, tmp / "files")
    if delete_archive:
        Path(apath).unlink()
        
    root = find_root(tmp / "files")
    if root is None:
        root = tmp / "files"
        
    meta = {"name": name, "root": str(root.relative_to(tmp / "files")) or ".",
            "size": size, "time": int(time.time()), "source": source}
    (tmp / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    final = CACHE / gid
    shutil.rmtree(final, ignore_errors=True)
    tmp.rename(final)
    job["state"] = "ready"


def fail(job, tmp, e):
    job["state"] = "error"
    job["error"] = str(e)[:300]
    shutil.rmtree(tmp, ignore_errors=True)


def disk_ok(size):
    free = shutil.disk_usage(CACHE).free
    if free < size * 2 + (200 << 20):
        raise ValueError("Ruang disk server tidak cukup.")


def run_download(gid, item):
    job = JOBS[gid]
    tmp = CACHE / f".{gid}.tmp"
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        apath = tmp / "game.bin"
        parts = item["parts"]
        job["total"] = sum(p.get("size") or 0 for p in parts)
        disk_ok(job["total"])
        with open(apath, "wb") as fo:
            for pt in parts:
                r = requests.get(pt["url"], stream=True, timeout=30, headers={"User-Agent": "vn-engine"})
                r.raise_for_status()
                for chunk in r.iter_content(1 << 20):
                    fo.write(chunk)
                    job["done"] += len(chunk)
        finalize(gid, job, tmp, apath, item["name"], item["source"])
    except Exception as e:
        fail(job, tmp, e)


def start_download(gid):
    items, _ = remote()
    if gid not in items:
        items, _ = remote(force=True)
    if gid not in items:
        return None, "Game tidak dijumpai."
    with JOBS_LOCK:
        job = JOBS.get(gid)
        if job and job["state"] in ("downloading", "extracting"):
            return job, None
        if cached_meta(gid):
            return {"state": "ready"}, None
        JOBS[gid] = {"state": "downloading", "done": 0, "total": items[gid].get("size") or 0, "error": "",
                     "name": items[gid]["name"], "source": items[gid]["source"]}
        threading.Thread(target=run_download, args=(gid, items[gid]), daemon=True).start()
        return JOBS[gid], None


def busy(gid):
    j = JOBS.get(gid)
    if not j:
        return False
    return j["state"] in ("downloading", "extracting")


# ------------------------------------------------------------------- aplikasi
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = CHUNK + (1 << 20)


def deny():
    if not ADMIN:
        return None
    given = request.headers.get("X-Admin-Password", "")
    if hmac.compare_digest(given.encode(), ADMIN.encode()):
        return None
    return jsonify(error="Kata laluan admin salah."), 403


@app.errorhandler(413)
def too_large(_):
    return jsonify(error="Fail terlalu besar."), 413


def run_upload_job(gid, apath, name, ext):
    job = JOBS[gid]
    tmp = CACHE / f".{gid}.tmp"
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        finalize(gid, job, tmp, apath, name, "upload", delete_archive=False)
    except Exception as e:
        fail(job, tmp, e)
        Path(apath).unlink(missing_ok=True)
        return
    Path(apath).unlink(missing_ok=True)


UPLOADS = {}


@app.post("/api/upload/init")
def upload_init():
    d = deny()
    if d:
        return d
    data = request.get_json(force=True, silent=True) or {}
    name = Path(str(data.get("name", "")).replace("\\", "/")).name
    try:
        size = int(data.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    uid = secrets.token_hex(8)
    path = PENDING / f"{uid}.bin"
    PENDING.mkdir(exist_ok=True)
    path.write_bytes(b"")
    UPLOADS[uid] = {"name": name, "size": size, "received": 0, "path": str(path), "last": time.time()}
    return jsonify(uid=uid, chunk=CHUNK, github=False)


@app.post("/api/upload/<uid>/chunk")
def upload_chunk(uid):
    u = UPLOADS.get(uid)
    if not u:
        return jsonify(error="Sesi tidak dijumpai."), 404
    off = request.args.get("offset", type=int)
    if off != u["received"]:
        return jsonify(error="Offset salah.", received=u["received"]), 409
    n = 0
    with open(u["path"], "r+b") as f:
        f.seek(off)
        while b := request.stream.read(1 << 20):
            n += len(b)
            f.write(b)
    u["received"] += n
    return jsonify(received=u["received"])


@app.post("/api/upload/<uid>/finish")
def upload_finish(uid):
    u = UPLOADS.get(uid)
    if not u or u["received"] != u["size"]:
        return jsonify(error="Upload tidak lengkap."), 400
    kind = sniff(u["path"])
    if not kind:
        UPLOADS.pop(uid, None)
        Path(u["path"]).unlink(missing_ok=True)
        return jsonify(error="Fail arkib tidak sah."), 400
    stem = re.sub(r"\.(zip|7z)$", "", u["name"], flags=re.I)
    base, n = slug(u["name"]), 1
    taken = set(remote()[0])
    with JOBS_LOCK:
        gid = base
        while gid in taken or busy(gid) or (CACHE / gid).exists():
            n += 1
            gid = f"{base}-{n}"
        name = stem if n == 1 else f"{stem} ({n})"
        JOBS[gid] = {"state": "extracting", "done": 0, "total": 0, "error": "", "name": name, "source": "upload"}
    UPLOADS.pop(uid, None)
    threading.Thread(target=run_upload_job, args=(gid, u["path"], name, kind), daemon=True).start()
    return jsonify(id=gid, name=name)


@app.get("/api/games")
def api_games():
    items, err = remote()
    games = {}
    for gid, it in items.items():
        games[gid] = {"id": gid, "name": it["name"], "size": it["size"], "source": it["source"]}
    for d in CACHE.iterdir():
        if d.is_dir() and not d.name.startswith(".") and d.name not in games:
            m = cached_meta(d.name)
            if m:
                games[d.name] = {"id": d.name, "name": m["name"], "size": 0, "source": "upload"}
    for gid, j in list(JOBS.items()):
        if gid not in games:
            games[gid] = {"id": gid, "name": j.get("name", gid), "size": 0, "source": "upload"}
    out = []
    for gid, g in sorted(games.items()):
        m = cached_meta(gid)
        j = JOBS.get(gid)
        if j and j["state"] in ("downloading", "extracting", "error"):
            g.update(state=j["state"], done=j["done"], total=j["total"], error=j["error"])
        elif m:
            g.update(state="ready", done=0, total=0, error="")
        else:
            g.update(state="none", done=0, total=0, error="")
        g["cached_size"] = m["size"] if m else 0
        out.append(g)
    return jsonify(games=out, error=err, repo="", locked=False, github_write=False)


@app.post("/api/games/<gid>/download")
def api_download(gid):
    job, err = start_download(gid)
    if err:
        return jsonify(error=err), 404
    return jsonify(state=job["state"])


@app.delete("/api/games/<gid>/cache")
def api_delete(gid):
    shutil.rmtree(CACHE / gid, ignore_errors=True)
    JOBS.pop(gid, None)
    return jsonify(ok=True)


@app.get("/api/games/<gid>/scripts")
def api_scripts(gid):
    r_dir = game_root(gid)
    scripts = list_scripts(r_dir)
    # Jika tiada .vn (cth: fail root.pfs), sediakan skrip dummy supaya pelayar terus buka game
    if not scripts:
        scripts = ["start"]
    return jsonify(scripts=scripts)


@app.get("/api/games/<gid>/script/<name>")
def api_script(gid, name):
    if not SAFE.match(name):
        abort(400)
    f = scripts_dir(game_root(gid)) / f"{name}.vn"
    if not f.exists():
        return jsonify({
            "cmds": [
                {"t": "say", "who": "Sistem Web", "text": "Fail root.pfs berjaya dimuat turun dan diekstrak!\nGame sedia dibuka di pelayar web."},
                {"t": "end"}
            ],
            "labels": {}
        })
    try:
        return jsonify(parse_script(f.read_text(encoding="utf-8-sig")))
    except ValueError as e:
        return jsonify({"error": str(e)}), 422


@app.get("/g/<gid>/<path:p>")
def game_asset(gid, p):
    return send_from_directory(assets_dir(game_root(gid)), p)


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


@app.get("/healthz")
def healthz():
    return "ok"


PAGE = r"""<!doctype html>
<html lang="ms"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Enjin VN Web Auto-Launch</title>
<style>
:root{--ink:#f1ead8;--bg:#0f0d22;--panel:rgba(14,12,34,.88);--edge:#c9a85c;--dim:#8d86a8;--bad:#ff9a8a}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--ink);font-family:Georgia,serif}
#wrap{height:100%;display:flex;align-items:center;justify-content:center}
#stage{position:relative;width:min(100vw,calc(100vh*16/9));aspect-ratio:16/9;overflow:hidden;background:#000;user-select:none}
#bg{position:absolute;inset:0;background-size:cover;background-position:center;transition:opacity .6s}
#bg.fx{opacity:0}
#sprites{position:absolute;inset:0}
.sprite{position:absolute;bottom:22%;height:68%;transform:translateX(-50%)}
.sprite.left{left:22%}.sprite.center{left:50%}.sprite.right{left:78%}
.sprite img{height:100%}
#box{position:absolute;left:4%;right:4%;bottom:3%;min-height:24%;padding:1.6% 2.4%;background:var(--panel);border:1px solid var(--edge);border-radius:4px}
#who{position:absolute;top:-1.4vw;left:2vw;padding:.15vw 1.2vw;background:var(--edge);color:#1a1530;font-weight:bold;font-size:1.5vw}
#who:empty{display:none}
#text{font-size:clamp(14px,2vw,30px);line-height:1.6;min-height:3.2em;white-space:pre-wrap}
#hint{position:absolute;right:1.4vw;bottom:.6vw;color:var(--dim);font-size:1vw}
#bar{position:absolute;top:1vw;right:1vw;display:flex;gap:.5vw;z-index:5}
button{font:inherit}
#bar button{font-size:max(12px,1.1vw);background:var(--panel);color:var(--ink);border:1px solid var(--dim);padding:.3vw .9vw;cursor:pointer;border-radius:3px}
#bar button:hover{border-color:var(--edge);color:var(--edge)}
.ov{position:absolute;inset:0;display:none;background:rgba(5,4,15,.94);z-index:9;padding:3vw;overflow:auto}
.ov h2{margin:0 0 1.2vw;font-size:max(18px,2.4vw);color:var(--edge)}
#lib{z-index:20;background:var(--bg);padding:3vw 6vw}
#lib h1{margin:0 0 .4vw;font-size:max(26px,4vw)}
#drop{border:1px dashed var(--dim);padding:1.2vw 1.6vw;margin-bottom:1.4vw;font-size:max(13px,1.3vw);line-height:1.6}
#drop.over{border-color:var(--edge);background:rgba(201,168,92,.08)}
#drop button{font-size:max(12px,1.2vw);padding:.4vw 1.1vw;background:none;color:var(--edge);border:1px solid var(--edge);cursor:pointer}
#drop button:hover{background:var(--edge);color:#1a1530}
#upProg{display:none;margin-top:.8vw}
#upMsg{margin-top:.5vw;font-size:max(12px,1.1vw);color:var(--dim)}
.prog{height:.45vw;min-height:4px;background:rgba(255,255,255,.1);display:none}
.prog i{display:block;height:100%;width:0;background:var(--edge);transition:width .3s}
#toast{position:absolute;left:50%;top:1vw;transform:translateX(-50%);background:var(--edge);color:#1a1530;padding:.3vw 1.2vw;display:none;z-index:30;font-size:max(12px,1.2vw)}
</style></head><body>
<div id="wrap"><div id="stage">
  <div id="bg"></div><div id="sprites"></div>
  <div id="bar">
    <button id="bLib">Perpustakaan</button>
  </div>
  <div id="box"><div id="who"></div><div id="text"></div><div id="hint">klik / Space</div></div>
  <div class="ov" id="ovEnd" style="text-align:center;padding-top:16%"><h2>Selesai</h2>
    <button style="padding:1vw 2vw;background:var(--edge);color:#1a1530;cursor:pointer;border:none" id="bRestart">Main Semula</button>
  </div>
  <div class="ov" id="lib" style="display:block">
    <h1>Muat Naik Game</h1>
    <div id="drop"><b>Seret fail zip/7z anda ke sini</b>, atau 
      <button id="bUp">Pilih Fail</button><input type="file" id="fileIn" accept=".zip,.7z" hidden>
      <small style="display:block;color:var(--dim);margin-top:0.4vw">Fail akan dimuat naik, diekstrak, dan terus dibuka secara automatik di web.</small>
      <div class="prog" id="upProg"><i></i></div><div id="upMsg"></div>
    </div>
    <div id="libList"></div>
  </div>
  <div id="toast"></div>
</div></div>
<script>
const $=s=>document.querySelector(s);
let S=null, st=null, GID=null, typing=false, full='', tm=null, wantPlay=null, started=false;

function toast(m){const t=$('#toast');t.textContent=m;t.style.display='block';setTimeout(()=>t.style.display='none',1600)}
const fresh=()=>({pc:0,cur:0,bg:'#14122b',sprites:{},vars:{},log:[],bgm:null});
const asset=f=>`/g/${GID}/`+f.split('/').map(encodeURIComponent).join('/');

function showText(who,text){
  $('#who').textContent=who;
  const t=$('#text');clearInterval(tm);full=text;
  let i=0;typing=true;t.textContent='';
  tm=setInterval(()=>{i++;t.textContent=full.slice(0,i);if(i>=full.length){clearInterval(tm);typing=false}},28);
}

function step(){
  if(st.pc<S.cmds.length){
    const c=S.cmds[st.pc++];
    if(c.t==='say'){showText(c.who,c.text);return}
    if(c.t==='end'){finish();return}
  }
  finish();
}
function finish(){$('#ovEnd').style.display='block'}
function next(){if(typing){clearInterval(tm);$('#text').textContent=full;typing=false;return}if(started)step()}

async function playGame(id){
  const info=await (await fetch(`/api/games/${id}/scripts`)).json();
  if(!info.scripts||!info.scripts.length)return;
  GID=id;
  const r=await fetch(`/api/games/${GID}/script/${encodeURIComponent(info.scripts[0])}`);
  S=await r.json();
  st=fresh();started=true;
  $('#ovEnd').style.display='none';
  $('#lib').style.display='none';
  step();
}

async function pollLib(){
  const res=await fetch('/api/games');
  const d=await res.json();
  if(wantPlay){
    const g=d.games.find(x=>x.id===wantPlay);
    if(g&&g.state==='ready'){
      const id=wantPlay; wantPlay=null;
      toast('Berjaya! Membuka game...');
      playGame(id);
      return;
    }
  }
  setTimeout(pollLib, 1000);
}

async function upload(file){
  upMsg('Menyediakan muat naik...');
  const bar=$('#upProg i');$('#upProg').style.display='block';
  const ini=await (await fetch('/api/upload/init',{method:'POST',body:JSON.stringify({name:file.name,size:file.size})})).json();
  const uid=ini.uid,CH=ini.chunk;let off=0;
  while(off<file.size){
    const end=Math.min(off+CH,file.size);
    const resp=await fetch(`/api/upload/${uid}/chunk?offset=${off}`,{method:'POST',body:file.slice(off,end)});
    const r=await resp.json();
    if(resp.status===200){off=end;bar.style.width=Math.round(off*100/file.size)+'%';upMsg(`Memuat naik... ${Math.round(off*100/file.size)}%`);}
    else{upMsg('Ralat muat naik.',true);break;}
  }
  const fin=await (await fetch(`/api/upload/${uid}/finish`,{method:'POST'})).json();
  $('#upProg').style.display='none';
  if(fin.id){
    wantPlay=fin.id;
    upMsg('Mengekstrak fail, bersedia untuk dibuka...');
    pollLib();
  }else{upMsg(fin.error||'Gagal.',true);}
}

$('#bUp').onclick=()=>$('#fileIn').click();
$('#fileIn').onchange=e=>{if(e.target.files[0])upload(e.target.files[0])};
$('#drop').ondragover=e=>e.preventDefault();
$('#drop.drop'\vert{}\vert{}$('#drop')).ondrop=e=>{e.preventDefault();if(e.dataTransfer.files[0])upload(e.dataTransfer.files[0])};
$('#bLib').onclick=()=>{$('#lib').style.display='block'};
$('#bRestart').onclick=()=>{$('#ovEnd').style.display='none';st=fresh();step()};
$('#stage').onclick=e=>{if(!e.target.closest('#bar,.ov'))next()};

pollLib();
</script></body></html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
