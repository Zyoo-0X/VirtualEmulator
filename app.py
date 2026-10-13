"""
Enjin Visual Novel - versi 2 Tab (Main & Upload)
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


def scripts_dir(root):
    return root / "scripts" if (root / "scripts").is_dir() else root


def assets_dir(root):
    return root / "assets" if (root / "assets").is_dir() else root


def list_scripts(root):
    return sorted(p.stem for p in scripts_dir(root).glob("*.vn") if SAFE.match(p.stem))


def find_root(base, max_depth=3):
    base = Path(base)
    if list_scripts(base):
        return base
    for dirpath, dirnames, _ in os.walk(base):
        depth = len(Path(dirpath).relative_to(base).parts)
        if depth >= max_depth:
            dirnames[:] = []
            continue
        dirnames.sort()
        for d in dirnames:
            cand = Path(dirpath) / d
            if d != "__MACOSX" and list_scripts(cand):
                return cand
    return None


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
        raise RuntimeError("Had kadar API GitHub dicapai. Tetapkan GITHUB_TOKEN.")
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
                    if len(assets) >= 30:
                        assets = gh_get(f"https://api.github.com/repos/{GH_REPO}/releases/{rel['id']}/assets",
                                        {"per_page": 100}) or assets
                    groups = {}
                    for a in assets:
                        m = re.match(r"^(.+\.(?:zip|7z))\.(\d{3})$", a["name"], re.I)
                        if m:
                            groups.setdefault(m.group(1), {})[int(m.group(2))] = a
                        else:
                            add(a["name"], a["size"], "release", parts=[asset_part(a)])
                    for base, d in groups.items():
                        idx = sorted(d)
                        if idx == list(range(1, len(idx) + 1)):
                            add(base, sum(d[i]["size"] for i in idx), "release",
                                parts=[asset_part(d[i]) for i in idx])
            except Exception as e:
                errors.append(f"Release: {e}")
        if GH_PATH:
            try:
                params = {"ref": GH_BRANCH} if GH_BRANCH else None
                data = gh_get(f"https://api.github.com/repos/{GH_REPO}/contents/{GH_PATH}", params)
                for f in data or []:
                    if f.get("type") == "file":
                        if GH_TOKEN:
                            part = {"url": f["url"], "accept": "application/vnd.github.raw", "size": f["size"], "fname": f["name"]}
                        elif f.get("download_url"):
                            part = {"url": f["download_url"], "accept": "*/*", "size": f["size"], "fname": f["name"]}
                        else:
                            continue
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
                    raise ValueError("Arkib mengandungi laluan tidak selamat.")
            for i in infos:
                z.extract(i, out)
        return total
    if kind == "7z":
        if py7zr is None:
            raise ValueError("Sokongan .7z tidak dipasang pada server (py7zr tiada).")
        with py7zr.SevenZipFile(apath, "r") as a:
            files = a.files
            total = sum(getattr(f, "uncompressed", 0) or 0 for f in files)
            if total > MAX_UNZIP:
                raise ValueError("Saiz selepas ekstrak melebihi had.")
            for f in files:
                if bad_path(out_r, f.filename) or getattr(f, "is_symlink", False):
                    raise ValueError("Arkib mengandungi laluan tidak selamat.")
            a.extractall(path=out)
        return total
    raise ValueError("Fail bukan arkib .zip atau .7z yang sah.")


def finalize(gid, job, tmp, apath, name, source, delete_archive=True):
    if not sniff(apath):
        raise ValueError("Fail bukan arkib .zip atau .7z yang sah.")
    job["state"] = "extracting"
    size = safe_extract(apath, tmp / "files")
    if delete_archive:
        Path(apath).unlink()
    root = find_root(tmp / "files") or (tmp / "files")
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
        raise ValueError(f"Ruang disk server tidak cukup (bebas {free >> 20} MB).")


def run_download(gid, item):
    job = JOBS[gid]
    tmp = CACHE / f".{gid}.tmp"
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        apath = tmp / "game.bin"
        if item["source"] == "local":
            src = Path(item["path"])
            job["total"] = src.stat().st_size
            disk_ok(job["total"])
            with open(src, "rb") as fi, open(apath, "wb") as fo:
                while chunk := fi.read(1 << 20):
                    fo.write(chunk)
                    job["done"] += len(chunk)
        else:
            parts = item["parts"]
            job["total"] = sum(p.get("size") or 0 for p in parts)
            if job["total"] > MAX_DL:
                raise ValueError("Saiz arkib melebihi had muat turun.")
            disk_ok(job["total"])
            with open(apath, "wb") as fo:
                for pt in parts:
                    hdr = {"User-Agent": "vn-engine", "Accept": pt.get("accept", "*/*")}
                    if GH_TOKEN and pt["url"].startswith("https://api.github.com/"):
                        hdr["Authorization"] = f"Bearer {GH_TOKEN}"
                    with requests.get(pt["url"], stream=True, timeout=30, allow_redirects=True, headers=hdr) as r:
                        r.raise_for_status()
                        for chunk in r.iter_content(1 << 20):
                            fo.write(chunk)
                            job["done"] += len(chunk)
                            if job["done"] > MAX_DL:
                                raise ValueError("Saiz arkib melebihi had muat turun.")
        finalize(gid, job, tmp, apath, item["name"], item["source"])
    except Exception as e:
        fail(job, tmp, e)


def start_download(gid):
    items, _ = remote()
    if gid not in items:
        items, _ = remote(force=True)
    if gid not in items:
        return None, "Game tidak dijumpai dalam sumber."
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
    gh = j.get("gh") or {}
    return j["state"] in ("downloading", "extracting") or gh.get("state") in ("waiting", "uploading")


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = CHUNK + (1 << 20)


def deny():
    if not ADMIN:
        return None
    given = request.headers.get("X-Admin-Password", "")
    if hmac.compare_digest(given.encode(), ADMIN.encode()):
        return None
    return jsonify(error="Kata laluan admin diperlukan atau salah."), 403


@app.errorhandler(413)
def too_large(_):
    return jsonify(error="Potongan terlalu besar."), 413


class FilePart:
    def __init__(self, path, off, length, cb):
        self.f = open(path, "rb")
        self.f.seek(off)
        self.length = self.left = length
        self.cb = cb

    def __len__(self):
        return self.length

    def read(self, n=-1):
        if self.left <= 0:
            return b""
        n = self.left if (n is None or n < 0) else min(n, self.left)
        b = self.f.read(min(n, 1 << 20))
        self.left -= len(b)
        self.cb(len(b))
        return b

    def __iter__(self):
        while b := self.read(1 << 20):
            yield b

    def close(self):
        self.f.close()


def gh_release_id():
    r = requests.get(f"https://api.github.com/repos/{GH_REPO}/releases/tags/{GH_TAG}",
                     headers=gh_headers(), timeout=20)
    if r.status_code == 200:
        return r.json()["id"]
    if r.status_code != 404:
        r.raise_for_status()
    r = requests.post(f"https://api.github.com/repos/{GH_REPO}/releases", headers=gh_headers(), timeout=20,
                      json={"tag_name": GH_TAG, "name": "Game library", "draft": False, "prerelease": False,
                            "body": "Disimpan oleh Enjin VN."})
    r.raise_for_status()
    return r.json()["id"]


def gh_assets(rid):
    out, page = [], 1
    while True:
        r = requests.get(f"https://api.github.com/repos/{GH_REPO}/releases/{rid}/assets",
                         headers=gh_headers(), params={"per_page": 100, "page": page}, timeout=20)
        r.raise_for_status()
        data = r.json()
        out += data
        if len(data) < 100:
            return out
        page += 1


def publish_github(gid, apath, ext, gh):
    size = os.path.getsize(apath)
    n = max(1, -(-size // PART))
    rid = gh_release_id()
    finals = [f"{gid}.{ext}"] if n == 1 else [f"{gid}.{ext}.{i + 1:03d}" for i in range(n)]
    uploaded, base_done = [], 0
    gh["state"] = "uploading"
    for i, fname in enumerate(finals):
        off, ln, tmpname = i * PART, min(PART, size - i * PART), fname + ".tmp"
        for attempt in range(3):
            try:
                for a in gh_assets(rid):
                    if a["name"] in (tmpname, fname):
                        requests.delete(a["url"], headers=gh_headers(), timeout=30)
                gh["done"] = base_done
                fp = FilePart(apath, off, ln, lambda k: gh.__setitem__("done", gh["done"] + k))
                try:
                    r = requests.post(f"https://uploads.github.com/repos/{GH_REPO}/releases/{rid}/assets",
                                      params={"name": tmpname}, data=fp, timeout=(30, 7200),
                                      headers={**gh_headers(), "Content-Type": "application/octet-stream"})
                finally:
                    fp.close()
                r.raise_for_status()
                uploaded.append((r.json()["id"], fname))
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(3 * (attempt + 1))
        base_done += ln
    for aid, fname in uploaded:
        r = requests.patch(f"https://api.github.com/repos/{GH_REPO}/releases/assets/{aid}",
                           headers=gh_headers(), json={"name": fname}, timeout=30)
        r.raise_for_status()


def run_upload_job(gid, apath, name, ext):
    job = JOBS[gid]
    tmp = CACHE / f".{gid}.tmp"
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        finalize(gid, job, tmp, apath, name, "upload", delete_archive=False)
    except Exception as e:
        fail(job, tmp, e)
        job.pop("gh", None)
        Path(apath).unlink(missing_ok=True)
        return
    if GH_WRITE:
        gh = job["gh"]
        try:
            publish_github(gid, apath, ext, gh)
            gh["state"] = "done"
            remote(force=True)
        except Exception as e:
            gh["state"] = "error"
            gh["error"] = str(e)[:200]
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
    if not re.search(r"\.(zip|7z)$", name, re.I):
        return jsonify(error="Hanya fail .zip atau .7z diterima."), 400
    if size <= 0:
        return jsonify(error="Fail kosong."), 400
    if size > MAX_DL:
        return jsonify(error=f"Fail terlalu besar."), 413
    uid = secrets.token_hex(8)
    path = PENDING / f"{uid}.bin"
    PENDING.mkdir(exist_ok=True)
    path.write_bytes(b"")
    UPLOADS[uid] = {"name": name, "size": size, "received": 0, "path": str(path), "last": time.time()}
    return jsonify(uid=uid, chunk=CHUNK, github=GH_WRITE)


@app.post("/api/upload/<uid>/chunk")
def upload_chunk(uid):
    d = deny()
    if d:
        return d
    u = UPLOADS.get(uid)
    if not u:
        return jsonify(error="Sesi upload tidak dijumpai."), 404
    off = request.args.get("offset", type=int)
    if off != u["received"]:
        return jsonify(error="Offset tidak sepadan.", received=u["received"]), 409
    n = 0
    try:
        with open(u["path"], "r+b") as f:
            f.seek(off)
            while b := request.stream.read(1 << 20):
                n += len(b)
                if off + n > u["size"]:
                    raise ValueError("Data melebihi saiz.")
                f.write(b)
    except Exception as e:
        with open(u["path"], "r+b") as f:
            f.truncate(u["received"])
        return jsonify(error=str(e)), 400
    u["received"] += n
    u["last"] = time.time()
    return jsonify(received=u["received"])


@app.post("/api/upload/<uid>/finish")
def upload_finish(uid):
    d = deny()
    if d:
        return d
    u = UPLOADS.get(uid)
    if not u:
        return jsonify(error="Sesi upload tidak dijumpai."), 404
    if u["received"] != u["size"]:
        return jsonify(error="Upload belum lengkap."), 400
    kind = sniff(u["path"])
    if not kind:
        UPLOADS.pop(uid, None)
        Path(u["path"]).unlink(missing_ok=True)
        return jsonify(error="Fail bukan arkib .zip atau .7z yang sah."), 400
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
        if GH_WRITE:
            JOBS[gid]["gh"] = {"state": "waiting", "done": 0, "total": u["size"], "error": ""}
    UPLOADS.pop(uid, None)
    threading.Thread(target=run_upload_job, args=(gid, u["path"], name, kind), daemon=True).start()
    return jsonify(id=gid, name=name)


@app.get("/api/games")
def api_games():
    items, err = remote(force=request.args.get("refresh") == "1")
    games = {}
    for gid, it in items.items():
        games[gid] = {"id": gid, "name": it["name"], "size": it["size"], "source": it["source"]}
    for d in CACHE.iterdir():
        if d.is_dir() and not d.name.startswith(".") and d.name not in games:
            m = cached_meta(d.name)
            if m:
                games[d.name] = {"id": d.name, "name": m["name"], "size": 0, "source": "cache"}
    for gid, j in list(JOBS.items()):
        if gid not in games:
            games[gid] = {"id": gid, "name": j.get("name", gid), "size": 0, "source": j.get("source", "upload")}
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
        g["gh"] = (j or {}).get("gh")
        out.append(g)
    return jsonify(games=out, error=err, repo=GH_REPO, locked=bool(ADMIN), github_write=GH_WRITE)


@app.post("/api/games/<gid>/download")
def api_download(gid):
    if not SAFE.match(gid):
        abort(400)
    job, err = start_download(gid)
    if err:
        return jsonify(error=err), 404
    return jsonify(state=job["state"])


@app.delete("/api/games/<gid>/cache")
def api_delete(gid):
    d = deny()
    if d:
        return d
    if not SAFE.match(gid):
        abort(400)
    shutil.rmtree(CACHE / gid, ignore_errors=True)
    JOBS.pop(gid, None)
    return jsonify(ok=True)


@app.delete("/api/cache")
def api_delete_all():
    d = deny()
    if d:
        return d
    for d in CACHE.iterdir():
        if d.is_dir() and not d.name.startswith("."):
            shutil.rmtree(d, ignore_errors=True)
    JOBS.clear()
    return jsonify(ok=True)


@app.get("/api/games/<gid>/scripts")
def api_scripts(gid):
    return jsonify(scripts=list_scripts(game_root(gid)))


@app.get("/api/games/<gid>/script/<name>")
def api_script(gid, name):
    if not SAFE.match(name):
        abort(400)
    f = scripts_dir(game_root(gid)) / f"{name}.vn"
    if not f.exists():
        abort(404)
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
<title>Enjin VN - 2 Tab</title>
<style>
:root{--ink:#f1ead8;--bg:#0f0d22;--panel:rgba(14,12,34,.88);--edge:#c9a85c;--dim:#8d86a8;--bad:#ff9a8a}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--ink);font-family:Georgia,serif}
#wrap{height:100%;display:flex;align-items:center;justify-content:center}
#stage{position:relative;width:min(100vw,calc(100vh*16/9));aspect-ratio:16/9;overflow:hidden;background:#000;user-select:none}
#bg{position:absolute;inset:0;background-size:cover;background-position:center}
#bar{position:absolute;top:1vw;right:1vw;display:flex;gap:.5vw;z-index:5}
button{font:inherit}
#bar button{font-size:max(12px,1.1vw);background:var(--panel);color:var(--ink);border:1px solid var(--dim);padding:.3vw .9vw;cursor:pointer;border-radius:3px}
#bar button:hover{border-color:var(--edge);color:var(--edge)}
.ov{position:absolute;inset:0;display:none;background:rgba(5,4,15,.96);z-index:20;padding:3vw 6vw;overflow:auto}
.ov h1{margin:0 0 .4vw;font-weight:normal;font-size:max(26px,4vw)}
.tabs{display:flex;gap:1vw;margin-bottom:1.5vw}
.tab-btn{background:none;border:1px solid var(--dim);color:var(--ink);padding:.5vw 1.5vw;cursor:pointer;font-size:max(14px,1.3vw);border-radius:3px}
.tab-btn.active{border-color:var(--edge);color:var(--edge);background:rgba(201,168,92,.1)}
.tab-content{display:none}
.tab-content.active{display:block}
.game{display:grid;grid-template-columns:1fr auto;gap:.4vw 1.4vw;align-items:center;padding:1vw 1.4vw;margin-bottom:.8vw;background:var(--panel);border:1px solid var(--dim)}
.gn{font-size:max(15px,1.8vw)}.gm{font-size:max(12px,1.1vw);color:var(--dim);margin-top:.2vw}
.acts button{font-size:max(12px,1.2vw);padding:.5vw 1.2vw;background:none;color:var(--edge);border:1px solid var(--edge);cursor:pointer;border-radius:3px}
.acts button:hover:not(:disabled){background:var(--edge);color:#1a1530}
.acts button:disabled{opacity:.5}
#drop{border:1px dashed var(--dim);padding:2vw;text-align:center;margin-bottom:1.5vw}
#drop.over{border-color:var(--edge);background:rgba(201,168,92,.08)}
#toast{position:absolute;left:50%;top:1vw;transform:translateX(-50%);background:var(--edge);color:#1a1530;padding:.3vw 1.2vw;display:none;z-index:30;font-size:max(12px,1.2vw)}
</style></head><body>
<div id="wrap"><div id="stage">
  <div id="bg"></div>
  <div id="bar">
    <button id="bHome">Menu Utama</button>
  </div>
  <div class="ov" id="ovMain" style="display:block">
    <h1>Enjin Visual Novel</h1>
    <div class="tabs">
      <button class="tab-btn active" data-tab="tabMain">Main Game</button>
      <button class="tab-btn" data-tab="tabUpload">Upload</button>
    </div>
    <div class="tab-content active" id="tabMain">
      <div id="libList"></div>
      <div id="libEmpty" style="color:var(--dim);margin-top:1vw">Tiada game dijumpai. Sila upload di tab Upload.</div>
      <div style="margin-top:1.5vw"><button id="bRefresh" class="tab-btn">Muat semula</button></div>
    </div>
    <div class="tab-content" id="tabUpload">
      <div id="drop">
        <p>Seret fail <b>.zip</b> atau <b>.7z</b> ke sini, atau</p>
        <button class="tab-btn" id="bUp">Pilih Fail</button>
        <input type="file" id="fileIn" accept=".zip,.7z" hidden>
        <div id="upProg" style="margin-top:1vw;display:none"><div style="height:6px;background:rgba(255,255,255,.2)"><i id="upBar" style="display:block;height:100%;width:0;background:var(--edge)"></i></div></div>
        <div id="upMsg" style="margin-top:.8vw;color:var(--dim)"></div>
      </div>
      <div style="margin-top:1.5vw">
        <input type="password" id="adminPw" placeholder="Kata laluan admin (jika ada)" style="padding:.5vw;background:var(--panel);color:var(--ink);border:1px solid var(--dim);width:20em">
      </div>
    </div>
  </div>
  <div id="toast"></div>
</div></div>
<script>
const $=s=>document.querySelector(s);
let GID=null, wantPlay=null;
function toast(m){const t=$('#toast');t.textContent=m;t.style.display='block';setTimeout(()=>t.style.display='none',1600)}

// Tab switching
document.querySelectorAll('.tab-btn[data-tab]').forEach(btn=>{
  btn.onclick=()=>{
    document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c=>c.classList.remove('active'));
    btn.classList.add('active');
    $(`#${btn.dataset.tab}`).classList.add('active');
  }
});

function adminH(){const v=$('#adminPw').value;return v?{'X-Admin-Password':v}:{}}

async function pollLib(){
  try{
    let r=await fetch('/api/games');let d=await r.json();
    let list=$('#libList');list.innerHTML='';
    d.games.forEach(g=>{
      let el=document.createElement('div');el.className='game';
      let statusText='';
      if(g.state==='downloading') statusText=`Memuat turun (${Math.round(g.done*100/g.total||0)}%)...`;
      else if(g.state==='extracting') statusText='Mengekstrak fail...';
      else if(g.state==='ready') statusText=`Sedia dimainkan (${(g.cached_size/1024/1024).toFixed(1)} MB)`;
      else if(g.state==='error') statusText='Ralat: '+g.error;
      else statusText='Belum dimuat turun';

      el.innerHTML=`<div><div class="gn">${g.name}</div><div class="gm">${statusText}</div></div>`+
                   `<div class="acts"><button class="main">${g.state==='ready'?'Main':'Muat Turun & Main'}</button>`+
                   `<button class="del" style="margin-left:.5vw;border-color:var(--bad);color:var(--bad)">Padam</button></div>`;
      
      el.querySelector('.main').onclick=()=>onMain(g.id,g.state);
      el.querySelector('.del').onclick=()=>onDelete(g.id,g.name);
      list.appendChild(el);
    });
    $('#libEmpty').style.display=d.games.length?'none':'block';
    
    if(wantPlay){
      let target=d.games.find(x=>x.id===wantPlay);
      if(target && target.state==='ready'){
        let id=wantPlay; wantPlay=null;
        playGame(id);
      }
    }
  }catch(e){console.error(e)}
}

async function onMain(id,state){
  if(state==='ready'){playGame(id);return}
  wantPlay=id;
  await fetch(`/api/games/${id}/download`,{method:'POST'});
  pollLib();
}

async function onDelete(id,name){
  if(!confirm(`Padam "${name}"?`))return;
  await fetch(`/api/games/${id}/cache`,{method:'DELETE',headers:adminH()});
  pollLib();
}

$('#bRefresh').onclick=()=>pollLib();
$('#bHome').onclick=()=>{$('#ovMain').style.display='block'};

// Upload logic
async function upload(file){
  let ini=await(await fetch('/api/upload/init',{method:'POST',headers:{'Content-Type':'application/json',...adminH()},body:JSON.stringify({name:file.name,size:file.size})})).json();
  if(ini.error){$('#upMsg').textContent=ini.error;return;}
  let uid=ini.uid, CH=ini.chunk, off=0;
  $('#upProg').style.display='block';
  while(off<file.size){
    let end=Math.min(off+CH, file.size);
    let chunkBlob=file.slice(off, end);
    let res=await fetch(`/api/upload/${uid}/chunk?offset=${off}`,{method:'POST',headers:adminH(),body:chunkBlob});
    let dat=await res.json();
    if(res.status===409){off=dat.received;continue;}
    if(!res.ok){$('#upMsg').textContent=dat.error||'Gagal upload';$('#upProg').style.display='none';return;}
    off=end;
    $('#upBar').style.width=Math.round(off*100/file.size)+'%';
    $('#upMsg').textContent=`Mengupload... ${Math.round(off*100/file.size)}%`;
  }
  let fin=await(await fetch(`/api/upload/${uid}/finish`,{method:'POST',headers:adminH()})).json();
  $('#upProg').style.display='none';
  if(fin.error){$('#upMsg').textContent=fin.error;return;}
  $('#upMsg').textContent='Upload selesai! Semak di tab Main Game.';
  wantPlay=fin.id;
  document.querySelectorAll('.tab-btn')[0].click();
  pollLib();
}

$('#bUp').onclick=()=>$('#fileIn').click();
$('#fileIn').onchange=e=>{if(e.target.files[0])upload(e.target.files[0])};
let dz=$('#drop');
['dragenter','dragover'].forEach(ev=>dz.addEventListener(ev,e=>{e.preventDefault();dz.classList.add('over')}));
['dragleave','drop'].forEach(ev=>dz.addEventListener(ev,e=>{e.preventDefault();dz.classList.remove('over')}));
dz.addEventListener('drop',e=>{if(e.dataTransfer.files[0])upload(e.dataTransfer.files[0])});

async function playGame(id){
  let info=await(await fetch(`/api/games/${id}/scripts`)).json();
  if(!info.scripts||!info.scripts.length){toast('Tiada skrip .vn');return}
  GID=id;
  $('#ovMain').style.display='none';
  toast('Memuatkan permainan...');
}

pollLib();
setInterval(pollLib, 10000);
</script></body></html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")),
            debug=os.environ.get("DEBUG") == "1", threaded=True)
