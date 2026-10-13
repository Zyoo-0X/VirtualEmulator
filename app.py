"""
Enjin Visual Novel (gaya Artemis) - versi GitHub + Render
---------------------------------------------------------
Enjin asal, ditulis dari awal. BUKAN pelaksanaan format Artemis sebenar.

Cara kerja:
  A. UPLOAD TERUS: seret / pilih fail .zip atau .7z di laman web (sehingga beberapa GB; fail
     dihantar berpotong-potong 16 MB). Server ekstrak dan game terus dimainkan; kawan anda
     buka pautan yang sama dan boleh main game itu juga.
     Jika GITHUB_REPO + GITHUB_TOKEN ditetapkan, arkib turut disimpan ke GitHub Release
     (dipecah automatik kepada bahagian <2 GB kerana had 2 GB/fail GitHub) supaya game kekal
     walaupun server restart. Selepas itu ia dimuat turun semula daripada GitHub bila perlu.
  B. GITHUB (pilihan): upload .zip/.7z ke GitHub (folder `games/` atau Release asset);
     senarai dikesan automatik, tekan "Muat turun & main".
  Butang "Padam cache" membuang game daripada server.

Pembolehubah persekitaran (Render -> Environment):
  GITHUB_REPO         wajib untuk sumber GitHub, cth. "namaanda/vn-games"
  GITHUB_GAMES_PATH   folder zip dalam repo (lalai: "games"); kosongkan untuk matikan
  GITHUB_BRANCH       cabang (lalai: cabang asal repo)
  GITHUB_USE_RELEASES "1" (lalai) untuk turut baca Release assets
  GITHUB_TOKEN        token (Contents: Read and write) - wajib untuk upload ke GitHub; juga
                      untuk repo persendirian / elak had kadar API
  GITHUB_RELEASE_TAG  tag Release penyimpanan game (lalai: "vn-games")
  ADMIN_PASSWORD      pilihan: jika ditetapkan, upload & padam memerlukan kata laluan ini
                      (bermain tetap bebas untuk semua orang)
  CACHE_DIR           lokasi cache (lalai: ./cache)
  MAX_DOWNLOAD_MB     had saiz arkib upload/muat turun (lalai 6144 = 6 GB)
  MAX_UNZIPPED_MB     had saiz selepas ekstrak (lalai 12288 = 12 GB)

Dalam arkib (.zip atau .7z), folder game mesti mengandungi skrip .vn (terus, atau dalam scripts/) dan
aset (terus, atau dalam assets/). Zip boleh ada satu folder induk. Lihat format skrip di bawah.

Format skrip (.vn):
    # komen
    @label nama
    @bg fail.png [fade]        (atau warna: @bg #223344)
    @show watak pose kedudukan (kiri | tengah | kanan) -> aset: watak_pose.png
    @hide watak
    @bgm fail.ogg              (@bgm stop untuk hentikan)
    @se fail.ogg
    @voice fail.ogg
    @set kunci=nilai           (atau kunci+=1)
    @if kunci==nilai -> label  (==, !=, >=, <=, >, <)
    @jump label
    @end
    ? Teks pilihan -> label    (baris ? berturut-turut = satu menu pilihan)
    Nama: dialog               (tanpa "Nama:" = naratif)

Jalankan setempat:  pip install -r requirements.txt && python app.py
Pada Render:        gunicorn app:app --workers 1 --threads 8 --timeout 300
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

try:  # 7z pilihan: app tetap boleh hidup tanpa py7zr (zip masih berfungsi)
    import py7zr
except ImportError:
    py7zr = None
from flask import Flask, Response, abort, jsonify, request, send_from_directory

BASE = Path(__file__).resolve().parent
CACHE = Path(os.environ.get("CACHE_DIR", BASE / "cache")).resolve()
LOCAL_ZIPS = BASE / "zips"  # pilihan: zip tempatan untuk ujian
GH_REPO = os.environ.get("GITHUB_REPO", "").strip()
GH_PATH = os.environ.get("GITHUB_GAMES_PATH", "games").strip("/ ")
GH_BRANCH = os.environ.get("GITHUB_BRANCH", "").strip()
GH_RELEASES = os.environ.get("GITHUB_USE_RELEASES", "1") != "0"
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
ADMIN = os.environ.get("ADMIN_PASSWORD", "")
MAX_DL = int(os.environ.get("MAX_DOWNLOAD_MB", "6144")) * 1024 * 1024
MAX_UNZIP = int(os.environ.get("MAX_UNZIPPED_MB", "12288")) * 1024 * 1024
GH_TAG = os.environ.get("GITHUB_RELEASE_TAG", "vn-games").strip() or "vn-games"
GH_WRITE = bool(GH_REPO and GH_TOKEN)   # boleh upload ke GitHub?
PART = 1900 * 1024 * 1024               # < 2 GiB, had satu aset Release
CHUNK = 16 * 1024 * 1024                # saiz potongan upload dari pelayar
PENDING = CACHE / ".pending"
LIST_TTL = 45  # saat; elak melebihi had kadar API GitHub

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
    """Cari folder game (yang ada .vn) di dalam hasil ekstrak zip."""
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
                "size": a["size"], "asset_id": a["id"]}

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
                        if m:  # bahagian berpecah: nama.zip.001, .002, ...
                            groups.setdefault(m.group(1), {})[int(m.group(2))] = a
                        else:
                            add(a["name"], a["size"], "release", parts=[asset_part(a)])
                    for base, d in groups.items():
                        idx = sorted(d)
                        if idx == list(range(1, len(idx) + 1)):  # hanya jika lengkap
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
                            part = {"url": f["url"], "accept": "application/vnd.github.raw", "size": f["size"]}
                        elif f.get("download_url"):
                            part = {"url": f["download_url"], "accept": "*/*", "size": f["size"]}
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


# ----------------------------------------------------- cache & muat turun
JOBS = {}  # gid -> {state, done, total, error}
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
    """Ekstrak .zip atau .7z dengan selamat. Pulangkan jumlah bait."""
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
    """Ekstrak arkib dalam tmp, cari root game, dan letak ke cache."""
    if not sniff(apath):
        raise ValueError("Fail bukan arkib .zip atau .7z yang sah.")
    job["state"] = "extracting"
    size = safe_extract(apath, tmp / "files")
    if delete_archive:
        Path(apath).unlink()
    root = find_root(tmp / "files")
    if root is None:
        base = tmp / "files"
        if any(base.rglob("*.pfs")) or any(base.rglob("*.pfs.000")) or any(base.rglob("system.ini")):
            raise ValueError("Ini game format Artemis (root.pfs). Enjin ini hanya menjalankan skrip .vn, "
                             "jadi game ini tidak boleh dimainkan di sini.")
        raise ValueError("Tiada fail .vn dijumpai dalam arkib (letak skrip dalam scripts/ atau terus di root).")
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


def run_extract(gid, tmp, apath, name, source):
    try:
        finalize(gid, JOBS[gid], tmp, apath, name, source)
    except Exception as e:
        fail(JOBS[gid], tmp, e)


def disk_ok(size):
    free = shutil.disk_usage(CACHE).free
    if free < size * 2 + (200 << 20):
        raise ValueError(f"Ruang disk server tidak cukup (bebas {free >> 20} MB, perlu kira-kira "
                         f"{(size * 2 + (200 << 20)) >> 20} MB termasuk hasil ekstrak).")


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
                for pt in parts:  # bahagian .001, .002 ... disambung berturutan
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


# ------------------------------------------------------------------- aplikasi
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = CHUNK + (1 << 20)  # satu potongan upload


def deny():
    """Semak kata laluan admin (jika ditetapkan). Pulangkan respons ralat atau None."""
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
    """Bahagian fail (offset, panjang) untuk dihantar ke GitHub tanpa memuatkan ke RAM."""

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
                            "body": "Disimpan oleh Enjin VN. Padam game melalui laman web."})
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
    """Upload arkib ke GitHub Release; pecah kepada bahagian <2 GB jika perlu."""
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
                for a in gh_assets(rid):  # buang sisa percubaan terdahulu
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
    # namakan semula selepas semua bahagian siap, supaya game separuh jalan tidak muncul dalam senarai
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


UPLOADS = {}  # uid -> {name, size, received, path, last}


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
        return jsonify(error=f"Fail terlalu besar (had {MAX_DL >> 20} MB)."), 413
    free = shutil.disk_usage(CACHE).free
    need = size * 2 + (200 << 20)
    if free < need:
        return jsonify(error=f"Ruang disk server tidak cukup: bebas {free >> 20} MB, perlu kira-kira "
                             f"{need >> 20} MB (arkib + hasil ekstrak)."), 507
    now = time.time()
    for k in [k for k, u in UPLOADS.items() if now - u["last"] > 6 * 3600]:  # buang sesi lapuk
        Path(UPLOADS.pop(k)["path"]).unlink(missing_ok=True)
    uid = secrets.token_hex(8)
    path = PENDING / f"{uid}.bin"
    PENDING.mkdir(exist_ok=True)
    path.write_bytes(b"")
    UPLOADS[uid] = {"name": name, "size": size, "received": 0, "path": str(path), "last": now}
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
    if off != u["received"]:  # klien perlu menyambung dari kedudukan sebenar
        return jsonify(error="Offset tidak sepadan.", received=u["received"]), 409
    n = 0
    try:
        with open(u["path"], "r+b") as f:
            f.seek(off)
            while b := request.stream.read(1 << 20):
                n += len(b)
                if off + n > u["size"]:
                    raise ValueError("Data melebihi saiz yang diisytiharkan.")
                f.write(b)
    except Exception as e:
        with open(u["path"], "r+b") as f:
            f.truncate(u["received"])
        return jsonify(error=str(e) or "Potongan gagal diterima."), 400
    if n == 0:
        return jsonify(error="Potongan kosong."), 400
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
        return jsonify(error="Upload belum lengkap.", received=u["received"]), 400
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
        while gid in taken or busy(gid) or (CACHE / gid).exists() or (gid in JOBS and JOBS[gid]["state"] == "error"):
            n += 1
            gid = f"{base}-{n}"
        name = stem if n == 1 else f"{stem} ({n})"
        JOBS[gid] = {"state": "extracting", "done": 0, "total": 0, "error": "", "name": name, "source": "upload"}
        if GH_WRITE:
            JOBS[gid]["gh"] = {"state": "waiting", "done": 0, "total": u["size"], "error": ""}
    UPLOADS.pop(uid, None)
    threading.Thread(target=run_upload_job, args=(gid, u["path"], name, kind), daemon=True).start()
    return jsonify(id=gid, name=name)


@app.delete("/api/games/<gid>/github")
def api_delete_github(gid):
    d = deny()
    if d:
        return d
    if not SAFE.match(gid):
        abort(400)
    if not GH_WRITE:
        return jsonify(error="Token GitHub tidak ditetapkan pada server."), 400
    if busy(gid):
        return jsonify(error="Sedang memuat turun / mengupload."), 409
    it = remote(force=True)[0].get(gid)
    if not it or it["source"] != "release":
        return jsonify(error="Game ini bukan daripada GitHub Release."), 404
    for pt in it["parts"]:
        r = requests.delete(f"https://api.github.com/repos/{GH_REPO}/releases/assets/{pt['asset_id']}",
                            headers=gh_headers(), timeout=30)
        if r.status_code not in (204, 404):
            return jsonify(error=f"GitHub menolak padam ({r.status_code})."), 502
    shutil.rmtree(CACHE / gid, ignore_errors=True)
    JOBS.pop(gid, None)
    remote(force=True)
    return jsonify(ok=True)


@app.get("/api/games")
def api_games():
    items, err = remote(force=request.args.get("refresh") == "1")
    games = {}
    for gid, it in items.items():
        games[gid] = {"id": gid, "name": it["name"], "size": it["size"], "source": it["source"]}
    for d in CACHE.iterdir():  # game yang ada dalam cache tetapi sudah tiada di sumber
        if d.is_dir() and not d.name.startswith(".") and d.name not in games:
            m = cached_meta(d.name)
            if m:
                games[d.name] = {"id": d.name, "name": m["name"], "size": 0,
                                 "source": m.get("source", "cache") if m.get("source") == "upload" else "cache"}
    for gid, j in list(JOBS.items()):  # kerja upload yang belum/tidak siap
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
    if busy(gid):
        return jsonify(error="Sedang memuat turun."), 409
    shutil.rmtree(CACHE / gid, ignore_errors=True)
    JOBS.pop(gid, None)
    return jsonify(ok=True)


@app.delete("/api/cache")
def api_delete_all():
    d = deny()
    if d:
        return d
    if any(busy(g) for g in list(JOBS)):
        return jsonify(error="Ada muat turun sedang berjalan."), 409
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
<title>Enjin VN</title>
<style>
:root{--ink:#f1ead8;--bg:#0f0d22;--panel:rgba(14,12,34,.88);--edge:#c9a85c;--dim:#8d86a8;--bad:#ff9a8a}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--ink);font-family:Georgia,"Times New Roman",serif}
#wrap{height:100%;display:flex;align-items:center;justify-content:center}
#stage{position:relative;width:min(100vw,calc(100vh*16/9));aspect-ratio:16/9;overflow:hidden;background:#000;user-select:none}
#bg{position:absolute;inset:0;background-size:cover;background-position:center;transition:opacity .6s}
#bg.fx{opacity:0}
#sprites{position:absolute;inset:0}
.sprite{position:absolute;bottom:22%;height:68%;transform:translateX(-50%)}
.sprite.left{left:22%}.sprite.center{left:50%}.sprite.right{left:78%}
.sprite img{height:100%}
.ph{height:100%;aspect-ratio:2/5;border:2px solid var(--edge);background:rgba(201,168,92,.12);
display:flex;align-items:flex-end;justify-content:center;padding:8px;font-size:1.1vw;color:var(--edge)}
#box{position:absolute;left:4%;right:4%;bottom:3%;min-height:24%;padding:1.6% 2.4%;background:var(--panel);border:1px solid var(--edge);border-radius:4px}
#who{position:absolute;top:-1.4vw;left:2vw;padding:.15vw 1.2vw;background:var(--edge);color:#1a1530;font-weight:bold;font-size:1.5vw}
#who:empty{display:none}
#text{font-size:clamp(14px,2vw,30px);line-height:1.6;min-height:3.2em;white-space:pre-wrap}
#hint{position:absolute;right:1.4vw;bottom:.6vw;color:var(--dim);font-size:1vw}
#bar{position:absolute;top:1vw;right:1vw;display:flex;gap:.5vw;z-index:5}
button{font:inherit}
#bar button{font-size:max(12px,1.1vw);background:var(--panel);color:var(--ink);border:1px solid var(--dim);padding:.3vw .9vw;cursor:pointer;border-radius:3px}
#bar button:hover,#bar button.on{border-color:var(--edge);color:var(--edge)}
button:focus-visible,.opt:focus-visible{outline:2px solid var(--edge);outline-offset:2px}
#choices{position:absolute;inset:0;display:none;flex-direction:column;justify-content:center;align-items:center;gap:1.4vw;background:rgba(5,4,15,.5)}
.opt{font-size:clamp(14px,2vw,28px);min-width:46%;padding:1vw 2vw;background:var(--panel);color:var(--ink);border:1px solid var(--edge);cursor:pointer;border-radius:3px}
.opt:hover{background:var(--edge);color:#1a1530}
.ov{position:absolute;inset:0;display:none;background:rgba(5,4,15,.94);z-index:9;padding:3vw;overflow:auto}
.ov h2{margin:0 0 1.2vw;font-weight:normal;font-size:max(18px,2.4vw);color:var(--edge)}
.ov p{margin:0 0 .8vw;font-size:max(13px,1.4vw);line-height:1.5}.ov p b{color:var(--edge)}
.ov button.close{position:absolute;top:1vw;right:1vw;background:none;color:var(--ink);border:1px solid var(--dim);padding:.3vw 1vw;cursor:pointer}
.slot{display:block;width:100%;text-align:left;font-size:max(13px,1.5vw);margin-bottom:.8vw;padding:.9vw 1.4vw;background:var(--panel);color:var(--ink);border:1px solid var(--dim);cursor:pointer}
.slot:hover{border-color:var(--edge)}
/* perpustakaan */
#lib{z-index:20;background:var(--bg);padding:3vw 6vw}
#lib h1{margin:0 0 .4vw;font-weight:normal;font-size:max(26px,4vw);letter-spacing:.05em}
#libSub{color:var(--dim);font-size:max(12px,1.2vw);margin-bottom:1.6vw}
#libErr{color:var(--bad);font-size:max(12px,1.2vw);margin-bottom:1vw}
.game{display:grid;grid-template-columns:1fr auto;gap:.4vw 1.4vw;align-items:center;padding:1vw 1.4vw;margin-bottom:.8vw;background:var(--panel);border:1px solid var(--dim)}
.gn{font-size:max(15px,1.8vw)}.gm{font-size:max(12px,1.1vw);color:var(--dim);margin-top:.2vw}
.gm.err{color:var(--bad)}
.acts{display:flex;gap:.6vw}
.acts button{font-size:max(12px,1.2vw);padding:.5vw 1.2vw;background:none;color:var(--edge);border:1px solid var(--edge);cursor:pointer}
.acts button:hover:not(:disabled){background:var(--edge);color:#1a1530}
.acts button:disabled{opacity:.5;cursor:default}
.acts .del{color:var(--bad);border-color:var(--bad)}
.acts .del:hover:not(:disabled){background:var(--bad);color:#1a1530}
#drop{border:1px dashed var(--dim);padding:1.2vw 1.6vw;margin-bottom:1.4vw;font-size:max(13px,1.3vw);line-height:1.6}
#drop.over{border-color:var(--edge);background:rgba(201,168,92,.08)}
#drop button{font-size:max(12px,1.2vw);padding:.4vw 1.1vw;background:none;color:var(--edge);border:1px solid var(--edge);cursor:pointer}
#drop button:hover{background:var(--edge);color:#1a1530}
#drop small{display:block;color:var(--dim);font-size:max(11px,1vw)}
#upProg{display:none;margin-top:.8vw}
#upMsg{margin-top:.5vw;font-size:max(12px,1.1vw);color:var(--dim)}#upMsg.err{color:var(--bad)}
#adminRow{display:none;margin-bottom:1.2vw}
#adminPw{font-size:max(12px,1.2vw);padding:.4vw .8vw;background:var(--panel);color:var(--ink);border:1px solid var(--dim);width:min(100%,22em)}
.prog{grid-column:1/-1;height:.45vw;min-height:4px;background:rgba(255,255,255,.1);display:none}
.prog i{display:block;height:100%;width:0;background:var(--edge);transition:width .3s}
.libtools{display:flex;gap:.6vw;margin-top:1.2vw}
.libtools button{font-size:max(12px,1.1vw);padding:.4vw 1.1vw;background:none;color:var(--ink);border:1px solid var(--dim);cursor:pointer}
.libtools button:hover{border-color:var(--edge);color:var(--edge)}
#libEmpty{color:var(--dim);font-size:max(13px,1.3vw);line-height:1.6}
#toast{position:absolute;left:50%;top:1vw;transform:translateX(-50%);background:var(--edge);color:#1a1530;padding:.3vw 1.2vw;display:none;z-index:30;font-size:max(12px,1.2vw)}
@media (prefers-reduced-motion:reduce){#bg,.prog i{transition:none}}
</style></head><body>
<div id="wrap"><div id="stage">
  <div id="bg"></div><div id="sprites"></div>
  <div id="bar">
    <button id="bAuto">Auto</button><button id="bSkip">Skip</button><button id="bLog">Log</button>
    <button id="bLib">Perpustakaan</button><button id="bSave">Simpan</button><button id="bLoad">Muat</button>
  </div>
  <div id="box"><div id="who"></div><div id="text"></div><div id="hint">klik / Space</div></div>
  <div id="choices"></div>
  <div class="ov" id="ovLog"><button class="close" data-close>Tutup</button><h2>Sejarah dialog</h2><div id="logBody"></div></div>
  <div class="ov" id="ovSlot"><button class="close" data-close>Tutup</button><h2 id="slotTitle"></h2><div id="slotBody"></div></div>
  <div class="ov" id="ovEnd" style="text-align:center;padding-top:16%"><h2>Tamat</h2>
    <button class="slot" style="width:auto;display:inline-block" id="bRestart">Main semula</button>
    <button class="slot" style="width:auto;display:inline-block" id="bEndLib">Perpustakaan</button></div>
  <div class="ov" id="lib" style="display:block">
    <button class="close" id="bBack" style="display:none">Kembali ke game</button>
    <h1>Enjin VN</h1><div id="libSub"></div><div id="libErr"></div>
    <div id="drop"><b>Upload game</b>: seret fail <b>.zip</b> atau <b>.7z</b> ke sini, atau
      <button id="bUp">pilih fail</button><input type="file" id="fileIn" accept=".zip,.7z" hidden>
      <small id="dropNote">Arkib mesti mengandungi folder game dengan skrip .vn. Fail besar (GB) dihantar berpotong-potong, jangan tutup tab semasa upload.</small>
      <div class="prog" id="upProg"><i></i></div><div id="upMsg"></div></div>
    <div id="adminRow"><input type="password" id="adminPw" placeholder="Kata laluan admin (untuk upload / padam)" autocomplete="off"></div>
    <div id="libList"></div><div id="libEmpty"></div>
    <div class="libtools"><button id="bRefresh">Muat semula senarai</button><button id="bClearAll">Padam semua cache</button></div>
  </div>
  <div id="toast"></div>
</div></div>
<script>
const $=s=>document.querySelector(s);
let S=null, st=null, GID=null, typing=false, full='', tm=null, auto=false, skip=false, autoT=null, choosing=false, started=false;
const bgm=new Audio(); bgm.loop=true;
const fresh=()=>({pc:0,cur:0,bg:'#14122b',sprites:{},vars:{},log:[],bgm:null});
const asset=f=>`/g/${GID}/`+f.split('/').map(encodeURIComponent).join('/');
const fmt=b=>b>=1e9?(b/1e9).toFixed(2)+' GB':b>=1e6?(b/1e6).toFixed(1)+' MB':b>=1e3?Math.round(b/1e3)+' KB':b+' B';
function toast(m){const t=$('#toast');t.textContent=m;t.style.display='block';setTimeout(()=>t.style.display='none',1600)}

// ================= enjin =================
function renderBg(fade){
  const b=$('#bg'),v=st.bg,apply=()=>{
    b.style.backgroundColor=v[0]==='#'?v:'#000';
    b.style.backgroundImage=v[0]==='#'?'none':`url("${asset(v)}")`;
  };
  if(fade){b.classList.add('fx');setTimeout(()=>{apply();b.classList.remove('fx')},600)}else apply();
}
function renderSprites(){
  const box=$('#sprites');box.innerHTML='';
  for(const [n,s] of Object.entries(st.sprites)){
    const d=document.createElement('div');d.className='sprite '+s.pos;
    const img=new Image();img.src=asset(`${n}_${s.pose}.png`);img.alt=n;
    img.onerror=()=>{const p=document.createElement('div');p.className='ph';p.textContent=n+' ('+s.pose+')';img.replaceWith(p)};
    d.appendChild(img);box.appendChild(d);
  }
}
function renderBgm(){
  if(!st.bgm){bgm.pause();return}
  const u=new URL(asset(st.bgm),location.href).href;
  if(bgm.src!==u)bgm.src=u;
  bgm.play().catch(()=>{});
}
function playSfx(f){const a=new Audio(asset(f));a.play().catch(()=>{});return a}

function showText(who,text,instant){
  $('#who').textContent=who;
  const t=$('#text');clearInterval(tm);full=text;
  if(instant||skip){t.textContent=text;typing=false;return afterText()}
  let i=0;typing=true;t.textContent='';
  tm=setInterval(()=>{i++;t.textContent=full.slice(0,i);if(i>=full.length)finishType()},28);
}
function finishType(){clearInterval(tm);$('#text').textContent=full;typing=false;afterText()}
function afterText(){clearTimeout(autoT);if(auto&&!skip)autoT=setTimeout(next,1200+full.length*45)}
const test=(a,op,b)=>({'==':a==b,'!=':a!=b,'>=':a>=b,'<=':a<=b,'>':a>b,'<':a<b})[op];

function step(){
  let guard=0;
  while(st.pc<S.cmds.length&&guard++<10000){
    const i=st.pc,c=S.cmds[st.pc++];
    switch(c.t){
      case 'say':st.cur=i;st.log.push({who:c.who,text:c.text});if(st.log.length>200)st.log.shift();showText(c.who,c.text);return;
      case 'choice':st.cur=i;showChoices(c);return;
      case 'bg':st.bg=c.v;renderBg(c.fade);break;
      case 'show':st.sprites[c.name]={pose:c.pose,pos:c.pos};renderSprites();break;
      case 'hide':delete st.sprites[c.name];renderSprites();break;
      case 'bgm':st.bgm=c.v==='stop'?null:c.v;renderBgm();break;
      case 'se':case 'voice':playSfx(c.v);break;
      case 'set':st.vars[c.k]=c.op==='+='?(Number(st.vars[c.k])||0)+c.v:c.v;break;
      case 'if':if(test(st.vars[c.k]??0,c.op,c.v))st.pc=S.labels[c.label];break;
      case 'jump':st.pc=S.labels[c.label];break;
      case 'end':finish();return;
    }
  }
  finish();
}
function finish(){auto=skip=false;syncBtns();$('#ovEnd').style.display='block'}
function showChoices(c){
  choosing=true;skip=false;syncBtns();
  const box=$('#choices');box.innerHTML='';box.style.display='flex';
  c.options.forEach(o=>{
    const b=document.createElement('button');b.className='opt';b.textContent=o.text;
    b.onclick=e=>{e.stopPropagation();box.style.display='none';choosing=false;
      st.log.push({who:'>',text:o.text});st.pc=S.labels[o.label];step()};
    box.appendChild(b);
  });
  box.firstChild.focus();
}
const anyOverlay=()=>[...document.querySelectorAll('.ov')].some(o=>o.style.display==='block');
function next(){if(!started||choosing||anyOverlay())return;if(typing){finishType();return}step()}
function syncBtns(){$('#bAuto').classList.toggle('on',auto);$('#bSkip').classList.toggle('on',skip)}
let skipT=null;
function setSkip(v){
  skip=v;clearInterval(skipT);
  if(v){auto=false;skipT=setInterval(()=>{if(choosing||anyOverlay())return;typing&&finishType();step()},60)}
  syncBtns();
}

// ================= simpan / muat (pelayar, per game) =================
const skey=n=>`vn:${GID}:slot${n}`;
function slotUI(mode){
  $('#slotTitle').textContent=mode==='save'?'Simpan ke slot':'Muat dari slot';
  const body=$('#slotBody');body.innerHTML='';
  for(let n=1;n<=6;n++){
    let d=null;try{d=JSON.parse(localStorage.getItem(skey(n))||'null')}catch(e){}
    const b=document.createElement('button');b.className='slot';
    const last=d&&d.log&&d.log.length?d.log[d.log.length-1].text.slice(0,50):'';
    b.textContent=`Slot ${n}: `+(d?(last||'(simpanan)'):'kosong');
    b.onclick=()=>{
      if(mode==='save'){
        try{localStorage.setItem(skey(n),JSON.stringify(st));toast('Disimpan')}catch(e){toast('Gagal simpan')}
      }else{
        if(!d)return;
        st=d;st.pc=st.cur;if(S.cmds[st.cur].t==='say')st.log.pop();
        renderBg();renderSprites();renderBgm();$('#choices').style.display='none';choosing=false;
        $('#ovEnd').style.display='none';toast('Dimuat');step();
      }
      $('#ovSlot').style.display='none';
    };
    body.appendChild(b);
  }
  $('#ovSlot').style.display='block';
}
function openLog(){
  const b=$('#logBody');b.innerHTML='';
  st.log.forEach(l=>{const p=document.createElement('p');
    if(l.who){const s=document.createElement('b');s.textContent=l.who+': ';p.appendChild(s)}
    p.appendChild(document.createTextNode(l.text));b.appendChild(p)});
  $('#ovLog').style.display='block';
}

// ================= perpustakaan: kesan, muat turun, cache =================
const rows={};let wantPlay=null,libTimer=null;
const SRC={github:'GitHub',release:'GitHub Release',local:'Tempatan',cache:'Dalam cache',upload:'Diupload'};
const libOpen=()=>$('#lib').style.display==='block';

function makeRow(g){
  const el=document.createElement('div');el.className='game';
  el.innerHTML='<div><div class="gn"></div><div class="gm"></div></div><div class="acts"><button class="main"></button><button class="del">Padam cache</button><button class="delgh del">Padam dari GitHub</button></div><div class="prog"><i></i></div>';
  const r={el,gn:el.querySelector('.gn'),gm:el.querySelector('.gm'),main:el.querySelector('.main'),del:el.querySelector('.del'),delgh:el.querySelector('.delgh'),prog:el.querySelector('.prog'),bar:el.querySelector('.prog i'),id:g.id,state:'none'};
  r.main.onclick=()=>onMain(r.id,r.state);
  r.del.onclick=()=>onDelete(r.id,r.gn.textContent);
  r.delgh.onclick=()=>onDeleteGh(r.id,r.gn.textContent);
  $('#libList').appendChild(el);rows[g.id]=r;return r;
}
function updRow(g){
  const r=rows[g.id]||makeRow(g);r.state=g.state;
  r.gn.textContent=g.name;
  const src=SRC[g.source]||g.source,pct=g.total?Math.min(100,Math.round(g.done*100/g.total)):0;
  let m;r.gm.className='gm';
  if(g.state==='downloading'){m=`Memuat turun ${fmt(g.done)}${g.total?' / '+fmt(g.total):''}${g.total?` (${pct}%)`:''}`}
  else if(g.state==='extracting'){m='Mengekstrak fail...'}
  else if(g.state==='ready'){m=`Dalam cache, ${fmt(g.cached_size)}`}
  else if(g.state==='error'){m='Ralat: '+g.error;r.gm.className='gm err'}
  else{m=(g.size?fmt(g.size)+', ':'')+src+', belum dimuat turun'}
  if(g.state==='extracting'&&!g.total)m='Mengekstrak fail...';
  const gh=g.gh,ghActive=!!gh&&(gh.state==='waiting'||gh.state==='uploading');
  if(gh){
    if(gh.state==='uploading'){const q=gh.total?Math.min(100,Math.round(gh.done*100/gh.total)):0;m+=` | Menyimpan ke GitHub ${fmt(gh.done)} / ${fmt(gh.total)} (${q}%)`}
    else if(gh.state==='waiting')m+=' | Menunggu giliran simpan ke GitHub';
    else if(gh.state==='done')m+=' | Disimpan di GitHub';
    else if(gh.state==='error'){m+=' | GitHub gagal: '+gh.error;}
  }
  r.gm.textContent=m;
  const active=g.state==='downloading'||g.state==='extracting'||ghActive;
  r.prog.style.display=active?'block':'none';
  r.bar.style.width=(ghActive&&gh.total?Math.min(100,Math.round(gh.done*100/gh.total)):g.state==='extracting'?100:pct)+'%';
  r.main.textContent=({none:'Muat turun & main',downloading:'Memuat turun...',extracting:'Mengekstrak...',ready:'Main',error:'Cuba lagi'})[g.state];
  r.main.disabled=g.state==='downloading'||g.state==='extracting';
  r.main.style.display=(g.state==='error'&&g.source==='upload')?'none':'';
  r.del.textContent=g.state==='error'?'Buang':'Padam cache';
  r.del.style.display=(g.state==='ready'||g.state==='error')?'':'none';
  r.del.disabled=active;
  r.delgh.style.display=(g.source==='release'&&LIB.github_write&&g.state!=='downloading'&&g.state!=='extracting')?'':'none';
  r.delgh.disabled=active;
}
let LIB={};
function renderLib(d){
  LIB=d;
  $('#libSub').textContent=d.repo?`Upload terus, atau dari github.com/${d.repo}`:'Upload game, kemudian kongsi pautan ini dengan kawan';
  $('#libErr').textContent=d.error||'';
  $('#adminRow').style.display=d.locked?'block':'none';
  $('#dropNote').textContent='Arkib mesti mengandungi folder game dengan skrip .vn. Fail besar (GB) dihantar berpotong-potong, jangan tutup tab semasa upload. '+(d.github_write?'Game turut disimpan ke GitHub (dipecah automatik jika >2 GB) supaya kekal.':'GitHub belum ditetapkan: game hanya disimpan sementara pada server.');
  const ids=new Set(d.games.map(g=>g.id));
  for(const id of Object.keys(rows)) if(!ids.has(id)){rows[id].el.remove();delete rows[id]}
  d.games.forEach(updRow);
  $('#libEmpty').textContent=d.games.length?'':(d.repo
    ?'Belum ada game. Upload fail .zip atau .7z di atas.'
    :'Belum ada game. Upload fail .zip atau .7z di atas.');
  if(wantPlay){
    const g=d.games.find(x=>x.id===wantPlay);
    if(g&&g.state==='ready'){const id=wantPlay;wantPlay=null;playGame(id)}
    else if(!g||g.state==='error'||g.state==='none'&&!g){wantPlay=null}
  }
}
async function pollLib(force){
  clearTimeout(libTimer);
  let d=null;
  try{d=await (await fetch('/api/games'+(force?'?refresh=1':''))).json();renderLib(d)}catch(e){$('#libErr').textContent='Tidak dapat hubungi server.'}
  if(libOpen()){
    const active=d&&d.games.some(g=>g.state==='downloading'||g.state==='extracting'||(g.gh&&(g.gh.state==='uploading'||g.gh.state==='waiting')));
    libTimer=setTimeout(()=>pollLib(false),active?600:30000);
  }
}
function openLib(){
  setSkip(false);auto=false;syncBtns();
  $('#lib').style.display='block';$('#bBack').style.display=started?'block':'none';pollLib(false);
}
async function onMain(id,state){
  if(state==='ready'){playGame(id);return}
  wantPlay=id;
  const r=await fetch(`/api/games/${id}/download`,{method:'POST'});
  if(!r.ok){wantPlay=null;toast((await r.json()).error||'Gagal')}
  pollLib(false);
}
async function onDelete(id,name){
  if(!confirm(`Padam "${name}" daripada server? Upload atau muat turun semula diperlukan untuk bermain.`))return;
  const r=await fetch(`/api/games/${id}/cache`,{method:'DELETE',headers:adminH()});
  if(!r.ok)toast((await r.json()).error||'Gagal');else{toast('Cache dipadam');if(GID===id){started=false;GID=null;bgm.pause()}}
  pollLib(false);
}
async function onDeleteGh(id,name){
  if(!confirm(`Padam "${name}" dari GitHub dan server? Ini kekal dan tidak boleh dibatalkan.`))return;
  const r=await fetch(`/api/games/${id}/github`,{method:'DELETE',headers:adminH()});
  if(!r.ok)toast((await r.json()).error||'Gagal');else{toast('Dipadam dari GitHub');if(GID===id){started=false;GID=null;bgm.pause()}}
  pollLib(true);
}
$('#bClearAll').onclick=async()=>{
  if(!confirm('Padam semua game dalam cache?'))return;
  const r=await fetch('/api/cache',{method:'DELETE',headers:adminH()});
  if(!r.ok)toast((await r.json()).error||'Gagal');else{toast('Semua cache dipadam');started=false;GID=null;bgm.pause();$('#bBack').style.display='none'}
  pollLib(false);
};
$('#bRefresh').onclick=()=>pollLib(true);

// ---- upload ----
$('#adminPw').value=sessionStorage.getItem('vn:admin')||'';
$('#adminPw').oninput=e=>sessionStorage.setItem('vn:admin',e.target.value);
function adminH(){const v=$('#adminPw').value;return v?{'X-Admin-Password':v}:{}}
function upMsg(t,err){const m=$('#upMsg');m.textContent=t;m.className=err?'err':''}
let uploading=false;
function sendChunk(uid,off,blob,onProg){
  return new Promise(res=>{
    const x=new XMLHttpRequest();x.open('POST',`/api/upload/${uid}/chunk?offset=${off}`);
    const h=adminH();for(const k in h)x.setRequestHeader(k,h[k]);
    x.upload.onprogress=e=>onProg(e.loaded);
    x.onload=()=>{let d={};try{d=JSON.parse(x.responseText)}catch(e){}res({status:x.status,d})};
    x.onerror=()=>res({status:0,d:{}});x.ontimeout=x.onerror;
    x.send(blob);
  });
}
async function jpost(url,body){
  const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json',...adminH()},body:body?JSON.stringify(body):undefined});
  let d={};try{d=await r.json()}catch(e){}return{ok:r.ok,d};
}
async function upload(file){
  if(uploading)return;
  if(!/\.(zip|7z)$/i.test(file.name)){upMsg('Hanya fail .zip atau .7z diterima.',true);return}
  uploading=true;upMsg('Menyediakan upload...');
  const bar=$('#upProg i');$('#upProg').style.display='block';bar.style.width='0';
  const stop=e=>{e.preventDefault();e.returnValue=''};window.addEventListener('beforeunload',stop);
  const fail=m=>{upMsg(m,true);$('#upProg').style.display='none'};
  try{
    const ini=await jpost('/api/upload/init',{name:file.name,size:file.size});
    if(!ini.ok){fail(ini.d.error||'Gagal memulakan upload.');return}
    const uid=ini.d.uid,CH=ini.d.chunk;let off=0,tries=0;
    while(off<file.size){
      const end=Math.min(off+CH,file.size),t0=Date.now();
      const r=await sendChunk(uid,off,file.slice(off,end),l=>{
        const p=off+l;bar.style.width=Math.round(p*100/file.size)+'%';
        upMsg(`Mengupload ${fmt(p)} / ${fmt(file.size)} (${Math.round(p*100/file.size)}%)`)});
      if(r.status===200){off=end;tries=0;continue}
      if(r.status===409&&typeof r.d.received==='number'){off=r.d.received;continue}
      if(r.status===403||r.status===404||r.status===413){fail(r.d.error||`Upload ditolak (${r.status}).`);return}
      if(++tries>4){fail(r.d.error||'Sambungan terputus. Cuba lagi.');return}
      upMsg(`Potongan gagal, mencuba semula (${tries}/4)...`);await new Promise(z=>setTimeout(z,1500*tries));
    }
    const fin=await jpost(`/api/upload/${uid}/finish`);
    $('#upProg').style.display='none';
    if(!fin.ok){upMsg(fin.d.error||'Gagal menamatkan upload.',true);return}
    upMsg('Upload siap, mengekstrak...');wantPlay=fin.d.id;pollLib(false);
  }finally{uploading=false;window.removeEventListener('beforeunload',stop)}
}
$('#bUp').onclick=()=>$('#fileIn').click();
$('#fileIn').onchange=e=>{if(e.target.files[0])upload(e.target.files[0]);e.target.value=''};
const dz=$('#drop');
['dragenter','dragover'].forEach(ev=>dz.addEventListener(ev,e=>{e.preventDefault();dz.classList.add('over')}));
['dragleave','drop'].forEach(ev=>dz.addEventListener(ev,e=>{e.preventDefault();dz.classList.remove('over')}));
dz.addEventListener('drop',e=>{if(e.dataTransfer.files[0])upload(e.dataTransfer.files[0])});
window.addEventListener('dragover',e=>e.preventDefault());window.addEventListener('drop',e=>e.preventDefault());
$('#bBack').onclick=()=>{$('#lib').style.display='none';clearTimeout(libTimer)};
$('#bLib').onclick=openLib;$('#bEndLib').onclick=()=>{$('#ovEnd').style.display='none';openLib()};

async function playGame(id){
  const info=await (await fetch(`/api/games/${id}/scripts`)).json();
  if(!info.scripts||!info.scripts.length){toast('Tiada skrip .vn');return}
  GID=id;await boot(info.scripts.includes('start')?'start':info.scripts[0]);
}
async function boot(name){
  const r=await fetch(`/api/games/${GID}/script/${encodeURIComponent(name)}`);S=await r.json();
  if(S.error){alert('Ralat skrip: '+S.error);return}
  bgm.pause();bgm.removeAttribute('src');
  clearInterval(tm);clearTimeout(autoT);setSkip(false);auto=false;syncBtns();
  $('#choices').style.display='none';choosing=false;$('#ovEnd').style.display='none';
  $('#lib').style.display='none';clearTimeout(libTimer);
  st=fresh();started=true;renderBg();renderSprites();step();
}
$('#bRestart').onclick=()=>{$('#ovEnd').style.display='none';st=fresh();renderBg();renderSprites();renderBgm();step()};

$('#stage').addEventListener('click',e=>{if(!e.target.closest('#bar,.ov,#choices'))next()});
document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>b.parentElement.style.display='none');
$('#bAuto').onclick=()=>{setSkip(false);auto=!auto;syncBtns();if(auto&&!typing)afterText()};
$('#bSkip').onclick=()=>setSkip(!skip);
$('#bLog').onclick=()=>started&&openLog();
$('#bSave').onclick=()=>started&&slotUI('save');
$('#bLoad').onclick=()=>started&&slotUI('load');
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){document.querySelectorAll('.ov:not(#lib)').forEach(o=>o.style.display='none');if(started&&libOpen())$('#bBack').click()}
  else if(e.key===' '||e.key==='Enter'){if(!['BUTTON','INPUT'].includes(document.activeElement.tagName)){e.preventDefault();next()}}
  else if(e.key==='Control')setSkip(true);
});
document.addEventListener('keyup',e=>{if(e.key==='Control')setSkip(false)});
pollLib(false);
</script></body></html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")),
            debug=os.environ.get("DEBUG") == "1", threaded=True)
