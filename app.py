"""
Enjin VN Web - Mod Game Sedia Ada (Pre-loaded di Web)
---------------------------------------------------------
Cara guna: Letak fail .zip game anda dalam folder 'zips' di sebelah app.py.
Buka web, ia akan terus terpapar dan anda hanya perlu tekan "Main".
"""
import os
import re
import json
import shutil
import zipfile
from pathlib import Path

try:  
    import py7zr
except ImportError:
    py7zr = None

from flask import Flask, Response, abort, jsonify, request, send_from_directory

BASE = Path(__file__).resolve().parent
CACHE = Path(os.environ.get("CACHE_DIR", BASE / "cache")).resolve()
LOCAL_ZIPS = BASE / "zips"  # Folder untuk letak game sedia ada

CACHE.mkdir(parents=True, exist_ok=True)
LOCAL_ZIPS.mkdir(parents=True, exist_ok=True)

SAFE = re.compile(r"^[\w\-]+$")

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
                             "pos": "center"})
            elif c == "hide" and a:
                cmds.append({"t": "hide", "name": a[0]})
            elif c in ("bgm", "se", "voice") and a:
                cmds.append({"t": c, "v": a[0]})
            elif c == "set":
                m = re.match(r"@set\s+(\w+)\s*(\+=|=)\s*(.+)$", line)
                if m:
                    cmds.append({"t": "set", "k": m.group(1), "op": m.group(2), "v": num(m.group(3).strip())})
            elif c == "if":
                m = re.match(r"@if\s+(\w+)\s*(==|!=|>=|<=|>|<)\s*(\S+)\s*->\s*(\S+)$", line)
                if m:
                    cmds.append({"t": "if", "k": m.group(1), "op": m.group(2), "v": num(m.group(3)), "label": m.group(4)})
            elif c == "jump" and a:
                cmds.append({"t": "jump", "label": a[0]})
            elif c == "end":
                cmds.append({"t": "end"})
            continue
        m = re.match(r"^([^:]{1,24}):\s+(.+)$", line)
        if m:
            cmds.append({"t": "say", "who": m.group(1).strip(), "text": m.group(2)})
        else:
            cmds.append({"t": "say", "who": "", "text": line})
    return {"cmds": cmds, "labels": labels}

def scripts_dir(root):
    return root / "scripts" if (root / "scripts").is_dir() else root

def assets_dir(root):
    return root / "assets" if (root / "assets").is_dir() else root

def list_scripts(root):
    return sorted(p.stem for p in scripts_dir(root).glob("*.vn") if SAFE.match(p.stem))

def find_root(base):
    base = Path(base)
    if list_scripts(base) or (base / "root.pfs").exists() or list(base.glob("root.pfs*")):
        return base
    for dirpath, dirnames, _ in os.walk(base):
        for d in dirnames:
            cand = Path(dirpath) / d
            if list_scripts(cand) or (cand / "root.pfs").exists() or list(cand.glob("root.pfs*")):
                return cand
    return base

def slug(name):
    stem = re.sub(r"\.(zip|7z)$", "", name, flags=re.I)
    return re.sub(r"[^\w\-]+", "-", stem).strip("-").lower() or "game"

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

def safe_extract(apath, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(apath):
        with zipfile.ZipFile(apath) as z:
            z.extractall(out)
    elif py7zr and py7zr.is_magic(apath):
        with py7zr.SevenZipFile(apath, "r") as a:
            a.extractall(path=out)

def extract_local_zips():
    """Automatik ekstrak fail zip dalam folder zips supaya sedia dimainkan."""
    if not LOCAL_ZIPS.is_dir():
        return
    for z in LOCAL_ZIPS.glob("*.*"):
        if not z.suffix.lower() in ('.zip', '.7z'):
            continue
        gid = slug(z.name)
        target = CACHE / gid
        if not target.exists():
            tmp = CACHE / f".{gid}.tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True, exist_ok=True)
            try:
                safe_extract(z, tmp / "files")
                root = find_root(tmp / "files")
                meta = {"name": z.stem, "root": str(root.relative_to(tmp / "files")) or ".", "time": int(time.time())}
                (tmp / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
                tmp.rename(target)
            except Exception:
                shutil.rmtree(tmp, ignore_errors=True)

app = Flask(__name__)

@app.get("/api/games")
def api_games():
    extract_local_zips()
    games = []
    for d in CACHE.iterdir():
        if d.is_dir() and not d.name.startswith("."):
            m = cached_meta(d.name)
            if m:
                games.append({"id": d.name, "name": m["name"], "state": "ready"})
    return jsonify(games=games)

@app.get("/api/games/<gid>/scripts")
def api_scripts(gid):
    r_dir = game_root(gid)
    scripts = list_scripts(r_dir)
    if not scripts:
        scripts = ["start"]
    return jsonify(scripts=scripts)

@app.get("/api/games/<gid>/script/<name>")
def api_script(gid, name):
    f = scripts_dir(game_root(gid)) / f"{name}.vn"
    if not f.exists():
        return jsonify({
            "cmds": [
                {"t": "say", "who": "Game Sedia", "text": "Game anda telah dimuatkan dari pelayan!\nTekan butang untuk mula."},
                {"t": "end"}
            ],
            "labels": {}
        })
    return jsonify(parse_script(f.read_text(encoding="utf-8-sig")))

@app.get("/g/<gid>/<path:p>")
def game_asset(gid, p):
    return send_from_directory(assets_dir(game_root(gid)), p)

@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")

PAGE = r"""<!doctype html>
<html lang="ms"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Enjin VN Web</title>
<style>
:root{--ink:#f1ead8;--bg:#0f0d22;--panel:rgba(14,12,34,.88);--edge:#c9a85c;--dim:#8d86a8}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--ink);font-family:Georgia,serif}
#wrap{height:100%;display:flex;align-items:center;justify-content:center}
#stage{position:relative;width:min(100vw,calc(100vh*16/9));aspect-ratio:16/9;overflow:hidden;background:#000;user-select:none}
#box{position:absolute;left:4%;right:4%;bottom:3%;min-height:24%;padding:1.6% 2.4%;background:var(--panel);border:1px solid var(--edge);border-radius:4px}
#who{position:absolute;top:-1.4vw;left:2vw;padding:.15vw 1.2vw;background:var(--edge);color:#1a1530;font-weight:bold;font-size:1.5vw}
#who:empty{display:none}
#text{font-size:clamp(14px,2vw,30px);line-height:1.6;min-height:3.2em;white-space:pre-wrap}
.ov{position:absolute;inset:0;background:rgba(5,4,15,.96);z-index:20;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:3vw}
h1{color:var(--edge);margin-bottom:1.5vw}
.game-btn{font-size:1.5vw;padding:1vw 2.5vw;background:var(--panel);color:var(--ink);border:2px solid var(--edge);cursor:pointer;border-radius:6px;margin:10px}
.game-btn:hover{background:var(--edge);color:#1a1530}
</style></head><body>
<div id="wrap"><div id="stage">
  <div id="box"><div id="who"></div><div id="text"></div></div>
  <div class="ov" id="lib">
    <h1>Pilih Game Anda</h1>
    <div id="gameList">Memuatkan senarai game...</div>
  </div>
  <div class="ov" id="ovEnd" style="display:none">
    <h1>Tamat</h1>
    <button class="game-btn" onclick="location.reload()">Kembali ke Menu</button>
  </div>
</div></div>
<script>
const $=s=>document.querySelector(s);
let S=null, st=null, GID=null, typing=false, full='';

async function loadGameList(){
  const res=await fetch('/api/games');
  const d=await res.json();
  const list=$('#gameList');
  list.innerHTML='';
  if(!d.games.length){
    list.innerHTML='<p style="color:var(--dim)">Tiada game dijumpai. Sila masukkan fail .zip game ke dalam folder <b>zips</b> di pelayan.</p>';
    return;
  }
  d.games.forEach(g=>{
    const b=document.createElement('button');
    b.className='game-btn';
    b.textContent='Main: '+g.name;
    b.onclick=()=>playGame(g.id);
    list.appendChild(b);
  });
}

async function playGame(id){
  const info=await (await fetch(`/api/games/${id}/scripts`)).json();
  GID=id;
  const r=await fetch(`/api/games/${GID}/script/${encodeURIComponent(info.scripts[0])}`);
  S=await r.json();
  st={pc:0};
  $('#lib').style.display='none';
  step();
}

function step(){
  if(st.pc<S.cmds.length){
    const c=S.cmds[st.pc++];
    if(c.t==='say'){
      $('#who').textContent=c.who;
      $('#text').textContent=c.text;
      return;
    }
  }
  $('#ovEnd').style.display='flex';
}

$('#stage').onclick=()=>{$('#lib').style.display==='none'&&step()};
loadGameList();
</script></body></html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
