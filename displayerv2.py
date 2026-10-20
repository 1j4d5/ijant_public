#!/usr/bin/env python3
"""IJANT Public Gallery Server - Multi-Page Edition.

Routes:
  /            -> Home landing page
  /people      -> People gallery grid
  /people/<id> -> Person detail page with photo grid + lightbox
  /images      -> All images grid
  /photo/<id>  -> Presigned URL redirect for images
  /api/...     -> JSON data endpoints
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.config import Config
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template_string
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

BASE = Path(__file__).resolve().parent
ENV_PATH = BASE / ".env"
load_dotenv(ENV_PATH)

B2_ENDPOINT_RAW = os.getenv("B2_ENDPOINT_URL", "s3.us-east-005.backblazeb2.com").strip()
if B2_ENDPOINT_RAW and not B2_ENDPOINT_RAW.startswith(("http://", "https://")):
    B2_ENDPOINT_URL = f"https://{B2_ENDPOINT_RAW}"
else:
    B2_ENDPOINT_URL = B2_ENDPOINT_RAW

B2_BUCKET = (os.getenv("B2_BUCKET_NAME") or os.getenv("B2_BUCKET") or "ijantdata").strip()
B2_KEY_ID = os.getenv("B2_KEY_ID", "").strip()
B2_APP_KEY = os.getenv("B2_APPLICATION_KEY", "").strip()
B2_REGION = os.getenv("B2_REGION", "us-west-004").strip()
PRESIGNED_TTL = int(os.getenv("B2_PRESIGNED_URL_TTL", "3600"))

NEON_DB_URL = os.getenv("NEON_DB_URL", "").strip()
SQLITE_FALLBACK = Path(os.getenv("IJANT_PUBLIC_DB_PATH", str(BASE / "public.ijant.sqlite"))).resolve()

HOST = os.getenv("GALLERY_HOST", "127.0.0.1")
PORT = int(os.getenv("GALLERY_PORT", "5050"))

app = Flask(__name__)

_S3_CLIENT = None

def get_s3_client():
    global _S3_CLIENT
    if _S3_CLIENT is None:
        if not all([B2_KEY_ID, B2_APP_KEY, B2_BUCKET]):
            abort(503, description="B2 configuration is incomplete.")
        _S3_CLIENT = boto3.client(
            "s3",
            endpoint_url=B2_ENDPOINT_URL,
            aws_access_key_id=B2_KEY_ID,
            aws_secret_access_key=B2_APP_KEY,
            region_name=B2_REGION,
            config=Config(signature_version="s3v4", max_pool_connections=25),
        )
    return _S3_CLIENT

_IMAGE_KEY_CACHE: Dict[int, str] = {}
_PRESIGNED_CACHE: Dict[int, Tuple[str, float]] = {}
_DB_POOL: Optional[ThreadedConnectionPool] = None

def get_db_conn():
    global _DB_POOL
    if not NEON_DB_URL:
        return None
    if _DB_POOL is None:
        try:
            _DB_POOL = ThreadedConnectionPool(1, 10, NEON_DB_URL, connect_timeout=10)
        except Exception:
            return None
    try:
        conn = _DB_POOL.getconn()
        if conn.closed:
            _DB_POOL.putconn(conn)
            conn = _DB_POOL.getconn()
        return conn
    except Exception:
        return None

def put_db_conn(conn):
    global _DB_POOL
    if _DB_POOL and conn:
        try:
            if not conn.closed:
                try:
                    conn.rollback()
                except Exception:
                    pass
            _DB_POOL.putconn(conn)
        except Exception:
            pass


# ── Shared CSS ────────────────────────────────────────────────────────────────
_CSS = """
:root {
  color-scheme: dark;
  --bg: #0b0d12; --card: #151923; --muted: #9ba4b5;
  --line: #282f3c; --accent: #a9f36b;
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:#f5f7fb; font:16px/1.45 system-ui,-apple-system,sans-serif; min-height:100vh; }
a { color:inherit; text-decoration:none; }
header { padding:14px clamp(16px,4vw,56px); border-bottom:1px solid var(--line); position:sticky; top:0; background:#0b0d12f2; backdrop-filter:blur(14px); z-index:10; }
.header-bar { display:flex; align-items:center; justify-content:space-between; gap:16px; flex-wrap:wrap; max-width:1600px; margin:auto; }
.logo { font-size:clamp(18px,2.5vw,24px); font-weight:700; letter-spacing:-0.5px; }
.logo span { color:var(--accent); }
nav { display:flex; gap:6px; flex-wrap:wrap; align-items:center; }
.nav-link { padding:7px 14px; border-radius:8px; border:1px solid transparent; font-size:14px; font-weight:500; color:var(--muted); transition:all .15s; cursor:pointer; }
.nav-link:hover, .nav-link.active { background:var(--card); border-color:var(--line); color:#f5f7fb; }
.nav-link.active { color:var(--accent); border-color:var(--accent); }
main { padding:32px clamp(16px,4vw,56px); max-width:1600px; margin:auto; }
.btn { border:1px solid var(--line); background:var(--card); color:inherit; padding:9px 16px; border-radius:10px; cursor:pointer; font-weight:500; font-size:14px; transition:all .15s; display:inline-flex; align-items:center; gap:6px; text-decoration:none; }
.btn:hover { border-color:var(--accent); color:var(--accent); }
.btn:active { transform:scale(0.98); }
.btn:disabled { opacity:0.5; cursor:wait; }
.btn-accent { background:var(--accent); color:#0b0d12; border-color:var(--accent); font-weight:700; }
.btn-accent:hover { background:#c2ff80; border-color:#c2ff80; color:#0b0d12; }
.hidden { display:none !important; }
.muted { color:var(--muted); }
.empty { color:var(--muted); padding:60px 0; font-size:16px; text-align:center; }
.page-hdr { margin-bottom:28px; }
.page-hdr h1 { font-size:clamp(24px,4vw,36px); margin:0 0 6px; }
.page-hdr p { color:var(--muted); font-size:14px; margin:0; }
.skel-grid { display:grid; gap:18px; }
.skel { border:1px solid var(--line); border-radius:14px; overflow:hidden; }
.skel-img { width:100%; background:#1c222e; animation:pulse 1.5s ease-in-out infinite; }
@keyframes pulse { 0%,100%{opacity:.4} 50%{opacity:.8} }
"""

_JS = """
function esc(s) {
  return String(s||'').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function imgUrl(id){ return '/photo/'+id; }
"""

def _nav(active):
    links = [("/","home","Home"),("/people","people","People"),("/images","images","Images")]
    items = "".join(
        '<a class="nav-link{}" href="{}">{}</a>'.format(" active" if k==active else "", h, l)
        for h,k,l in links
    )
    return (
        '<header><div class="header-bar">'
        '<a href="/" class="logo">IJANT <span>Gallery</span></a>'
        '<nav>{}</nav></div></header>'
    ).format(items)


# ════════════════════════════════════════════════════════
#  HOME  /
# ════════════════════════════════════════════════════════
def _home_page():
    return """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>IJANT Gallery</title>
<style>""" + _CSS + """
.hero { text-align:center; padding:80px 24px 60px; }
.hero h1 { font-size:clamp(36px,7vw,72px); font-weight:800; letter-spacing:-2px; margin:0 0 18px;
  background:linear-gradient(135deg,#f5f7fb 30%,var(--accent)); -webkit-background-clip:text;
  -webkit-text-fill-color:transparent; background-clip:text; }
.hero p { font-size:clamp(16px,2vw,20px); color:var(--muted); max-width:520px; margin:0 auto 36px; }
.hero-actions { display:flex; gap:14px; justify-content:center; flex-wrap:wrap; }
.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:18px; max-width:620px; margin:56px auto 0; }
.stat { background:var(--card); border:1px solid var(--line); border-radius:18px; padding:26px 22px; text-align:center; }
.stat-n { font-size:clamp(34px,5vw,52px); font-weight:800; color:var(--accent); line-height:1; display:block; }
.stat-l { color:var(--muted); font-size:14px; margin-top:6px; display:block; }
.features { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:18px; margin:44px 0 0; }
.feature { background:var(--card); border:1px solid var(--line); border-radius:16px; padding:24px 20px;
  transition:border-color .15s, transform .15s; }
.feature:hover { border-color:var(--accent); transform:translateY(-2px); }
.feature-icon { font-size:28px; margin-bottom:10px; }
.feature h3 { margin:0 0 8px; font-size:16px; }
.feature p { margin:0; color:var(--muted); font-size:14px; line-height:1.6; }
.sec-hdr { margin:48px 0 20px; }
.sec-hdr h2 { font-size:22px; margin:0 0 4px; }
.sec-hdr p { color:var(--muted); font-size:14px; margin:0; }
</style></head><body>""" + _nav("home") + """
<main>
<div class="hero">
  <h1>IJANT Gallery</h1>
  <p>Explore people and photos from the IJANT archive.<br><small style="font-size:13px;color:var(--muted)">powered by ijads pink computing unit</small></p>
  <div class="hero-actions">
    <a class="btn btn-accent" href="/people">People</a>
    <a class="btn" href="/images">All Images</a>
  </div>
  <div class="stats">
    <div class="stat"><span class="stat-n" id="sP">-</span><span class="stat-l">People</span></div>
    <div class="stat"><span class="stat-n" id="sI">-</span><span class="stat-l">Photos</span></div>
    <div class="stat"><span class="stat-n" id="sF">-</span><span class="stat-l">Face Tags</span></div>
  </div>
</div>
<div class="sec-hdr"><h2>Browse the archive</h2></div>
<div class="features">
  <a class="feature" href="/people">
    <div class="feature-icon">&#128101;</div>
    <h3>People</h3>
    <p>Browse every person in the archive with their cover photo and photo count.</p>
  </a>
  <a class="feature" href="/images">
    <div class="feature-icon">&#128247;</div>
    <h3>All Images</h3>
    <p>Scroll through every photo in the collection. Click to view full screen.</p>
  </a>
  <div class="feature">
    <div class="feature-icon">&#128250;</div>
    <h3>Face Detection HUD</h3>
    <p>Green bounding boxes highlight detected faces. Toggle on/off inside any photo viewer.</p>
  </div>
</div>
</main>
<script>""" + _JS + """
(async()=>{
  try{
    const r=await fetch('/api/stats');const d=await r.json();
    if(d.ok){
      document.getElementById('sP').textContent=d.data.people.toLocaleString();
      document.getElementById('sI').textContent=d.data.images.toLocaleString();
      document.getElementById('sF').textContent=d.data.face_boxes.toLocaleString();
    }
  }catch(e){}
})();
</script></body></html>"""


# ════════════════════════════════════════════════════════
#  PEOPLE  /people
# ════════════════════════════════════════════════════════
def _people_page():
    skels = '<div class="skel"><div class="skel-img" style="aspect-ratio:4/5"></div><div style="padding:11px"><div style="height:14px;background:#1c222e;border-radius:6px;animation:pulse 1.5s infinite;margin-bottom:7px"></div><div style="height:12px;width:55%;background:#1c222e;border-radius:6px;animation:pulse 1.5s infinite"></div></div></div>' * 12
    return """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>People - IJANT Gallery</title>
<style>""" + _CSS + """
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(190px,1fr)); gap:18px; }
.skel-grid { grid-template-columns:repeat(auto-fill,minmax(190px,1fr)); }
.person { border:1px solid var(--line); border-radius:16px; background:var(--card); overflow:hidden;
  color:inherit; cursor:pointer; transition:transform .15s,border-color .15s,box-shadow .15s; display:block; }
.person:hover { transform:translateY(-3px); border-color:var(--accent); box-shadow:0 8px 24px rgba(0,0,0,.5); }
.person img { width:100%; aspect-ratio:4/5; object-fit:cover; display:block; background:#1c222e; }
.person .nophoto { aspect-ratio:4/5; display:grid; place-items:center; color:var(--muted); font-size:40px; background:#12161f; }
.person .meta { padding:13px; }
.person strong { display:block; font-size:16px; margin-bottom:3px; }
.search-row { display:flex; gap:10px; margin-bottom:22px; flex-wrap:wrap; }
.search-row input { flex:1; min-width:200px; background:var(--card); border:1px solid var(--line);
  border-radius:10px; color:#f5f7fb; padding:9px 14px; font-size:14px; outline:none; transition:border-color .15s; }
.search-row input:focus { border-color:var(--accent); }
@media(max-width:600px){ .grid,.skel-grid{ grid-template-columns:repeat(2,1fr); gap:10px; } }
</style></head><body>""" + _nav("people") + """
<main>
<div class="page-hdr"><h1>People</h1><p id="subtitle"><small>(let ijads pink computing unit process)</small></p></div>
<div class="search-row"><input type="search" id="q" placeholder="Search by name..." autocomplete="off"></div>
<div id="skel" class="skel-grid" style="display:grid">""" + skels + """</div>
<div id="grid" class="grid hidden"></div>
<div id="empty" class="empty hidden">No people found.</div>
</main>
<script>""" + _JS + """
let all=[];
function render(list){
  const g=document.getElementById('grid');
  const em=document.getElementById('empty');
  if(!list.length){ g.classList.add('hidden'); em.classList.remove('hidden'); return; }
  em.classList.add('hidden');
  g.innerHTML=list.map(p=>`
    <a class="person" href="/people/${p.id}">
      ${p.cover_photo_id
        ? `<img loading="lazy" src="${imgUrl(p.cover_photo_id)}" alt="${esc(p.name)}">`
        : `<div class="nophoto">?</div>`
      }
      <div class="meta"><strong>${esc(p.name)}</strong><span class="muted">${p.photo_count} photo${p.photo_count===1?'':'s'}</span></div>
    </a>`).join('');
  g.classList.remove('hidden');
}
document.getElementById('q').addEventListener('input',e=>{
  const q=e.target.value.trim().toLowerCase();
  render(q?all.filter(p=>p.name.toLowerCase().includes(q)):all);
});
(async()=>{
  try{
    const r=await fetch('/api/public/people');const d=await r.json();
    if(!d.ok) throw Error(d.error||'Error');
    all=d.data;
    document.getElementById('skel').classList.add('hidden');
    document.getElementById('subtitle').textContent=all.length+' people in the archive';
    render(all);
  }catch(e){
    document.getElementById('skel').classList.add('hidden');
    document.getElementById('subtitle').textContent='Failed: '+e.message;
  }
})();
</script></body></html>"""


# ════════════════════════════════════════════════════════
#  PERSON DETAIL  /people/<id>
# ════════════════════════════════════════════════════════
def _person_page():
    skels = '<div class="skel"><div class="skel-img" style="aspect-ratio:1"></div></div>' * 12
    return r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Person - IJANT Gallery</title>
<style>""" + _CSS + r"""
.back-bar { display:flex; align-items:center; gap:14px; margin-bottom:26px; }
.back-bar h1 { font-size:clamp(22px,3.5vw,32px); margin:0; }
.photos { display:grid; grid-template-columns:repeat(auto-fill,minmax(180px,1fr)); gap:12px; }
.skel-grid { grid-template-columns:repeat(auto-fill,minmax(180px,1fr)); gap:12px; }
.photo-btn { padding:0; border:1px solid var(--line); background:#161b25; border-radius:12px;
  overflow:visible; cursor:zoom-in; transition:transform .15s,border-color .15s; display:block; width:100%; }
.photo-btn:hover { transform:translateY(-2px); border-color:var(--accent); }
.frame { position:relative; display:block; width:100%; aspect-ratio:1; overflow:hidden; border-radius:11px; }
.frame img { width:100%; height:100%; object-fit:contain; display:block; background:#10141d; }

/* Face Box HUD */
.face-box {
  position:absolute; border:2.5px solid var(--accent);
  box-shadow:0 0 0 1px rgba(0,0,0,.7),0 0 12px rgba(169,243,107,.55);
  border-radius:3px; pointer-events:none; z-index:5; display:none;
}
.face-box.placed { display:block; }

/* Lightbox */
#lightbox { position:fixed; inset:0; background:rgba(3,4,7,.95); backdrop-filter:blur(10px);
  z-index:100; display:flex; align-items:center; justify-content:center; padding:70px 70px 55px; user-select:none; }
#lightbox.hidden { display:none !important; }
#viewer { position:relative; display:inline-block; max-width:100%; max-height:100%; line-height:0; }
#viewer img { max-width:100%; max-height:calc(100vh - 130px); object-fit:contain; display:block;
  border-radius:6px; box-shadow:0 12px 40px rgba(0,0,0,.8); }
#viewer .face-box {
  position:absolute; border:3px solid var(--accent);
  box-shadow:0 0 0 1.5px rgba(0,0,0,.85),0 0 18px rgba(169,243,107,.7);
  border-radius:4px; pointer-events:auto; cursor:default; z-index:50; display:none;
}
#viewer .face-box.placed { display:block; }
#viewer .face-box::after { content:attr(data-name); position:absolute; bottom:calc(100%+4px); left:0;
  background:rgba(10,12,16,.9); color:var(--accent); font-size:11px; font-weight:600;
  padding:2px 8px; border-radius:4px; white-space:nowrap; border:1px solid var(--line);
  opacity:0; transition:opacity .15s; pointer-events:none; }
#viewer .face-box:hover::after { opacity:1; }
#lightbox.boxes-off .face-box, .boxes-off .face-box { display:none !important; }
.lb-ctrl { position:absolute; border:1px solid var(--line); background:rgba(23,27,36,.9);
  color:white; border-radius:10px; padding:10px 16px; cursor:pointer; font-size:14px;
  font-weight:500; transition:all .15s; z-index:110; }
.lb-ctrl:hover { border-color:var(--accent); color:var(--accent); }
#lbClose { top:14px; right:14px; }
#lbToggle { top:14px; right:122px; }
#lbPrev { left:14px; top:50%; transform:translateY(-50%); font-size:20px; padding:14px 18px; }
#lbNext { right:14px; top:50%; transform:translateY(-50%); font-size:20px; padding:14px 18px; }
#lbCounter { position:absolute; bottom:14px; left:50%; transform:translateX(-50%); color:#d3d7e0;
  font-size:13px; background:rgba(23,27,36,.7); padding:5px 14px; border-radius:20px;
  border:1px solid var(--line); white-space:nowrap; }
@media(max-width:600px){
  .photos,.skel-grid{ grid-template-columns:repeat(2,1fr); gap:8px; }
  #lightbox{ padding:60px 8px 50px; }
  #lbPrev{ left:6px; } #lbNext{ right:6px; }
  #lbToggle{ right:90px; padding:8px 12px; font-size:12px; }
  #lbClose{ padding:8px 12px; font-size:12px; }
}
</style></head><body>""" + _nav("people") + r"""
<main>
<div class="back-bar">
  <a class="btn" href="/people">&#8592; All People</a>
  <div><h1 id="personName">Loading...</h1><div class="muted" id="photoCount"></div></div>
</div>
<div id="skel" class="skel-grid" style="display:grid">""" + skels + r"""</div>
<div id="photoGrid" class="photos hidden"></div>
<div id="empty" class="empty hidden">No photos for this person.</div>
</main>

<div id="lightbox" class="hidden" role="dialog" aria-modal="true">
  <button class="lb-ctrl" id="lbClose">&#10005; Close</button>
  <button class="lb-ctrl" id="lbToggle">Hide face boxes</button>
  <button class="lb-ctrl" id="lbPrev">&#8592;</button>
  <div id="viewer"></div>
  <button class="lb-ctrl" id="lbNext">&#8594;</button>
  <div id="lbCounter"></div>
</div>

<script>""" + _JS + r"""
const PID = Number(location.pathname.split('/').pop());
let photos=[], idx=0, personName='';

// ── Face box positioning ─────────────────────────────────────────────────────
// CRITICAL: call this AFTER container is visible (not display:none)!
function positionBoxes(container, img) {
  if(!container || !img) return;
  if(!img.naturalWidth || !img.naturalHeight) return;
  const iw = img.clientWidth, ih = img.clientHeight;
  if(!iw || !ih) { requestAnimationFrame(()=>positionBoxes(container,img)); return; }
  const scale = Math.min(iw/img.naturalWidth, ih/img.naturalHeight);
  const rw = img.naturalWidth*scale, rh = img.naturalHeight*scale;
  const ox = (iw-rw)/2, oy = (ih-rh)/2;
  container.querySelectorAll('.face-box').forEach(b=>{
    const x1=parseFloat(b.dataset.x1)||0, y1=parseFloat(b.dataset.y1)||0;
    const x2=parseFloat(b.dataset.x2)||0, y2=parseFloat(b.dataset.y2)||0;
    b.style.left   = (ox + x1*scale)+'px';
    b.style.top    = (oy + y1*scale)+'px';
    b.style.width  = Math.max(2,(x2-x1)*scale)+'px';
    b.style.height = Math.max(2,(y2-y1)*scale)+'px';
    b.classList.add('placed');
  });
}

// ── Lightbox ─────────────────────────────────────────────────────────────────
let _ro = null;
function showPhoto(i) {
  if(!photos.length) return;
  idx = ((i%photos.length)+photos.length)%photos.length;
  const ph = photos[idx];
  const boxes = (ph.faces||[]).map(f=>
    `<div class="face-box" data-x1="${f.x1}" data-y1="${f.y1}" data-x2="${f.x2}" data-y2="${f.y2}" data-name="${esc(personName)}"></div>`
  ).join('');
  document.getElementById('viewer').innerHTML = `<img id="lbImg" alt="Photo" src="${imgUrl(ph.id)}">${boxes}`;

  // Show lightbox FIRST so dimensions are available (this is the critical fix!)
  document.getElementById('lightbox').classList.remove('hidden');
  document.body.style.overflow='hidden';

  const img = document.getElementById('lbImg');
  const viewer = document.getElementById('viewer');
  if(_ro){ try{_ro.disconnect();}catch(e){} _ro=null; }
  const place = () => {
    positionBoxes(viewer, img);
    requestAnimationFrame(()=>positionBoxes(viewer, img));
  };
  if(img.complete && img.naturalWidth>0){ place(); }
  else { img.addEventListener('load', place, {once:true}); }
  try{
    _ro = new ResizeObserver(()=>positionBoxes(viewer,img));
    _ro.observe(img);
  }catch(e){}
  document.getElementById('lbCounter').textContent = (idx+1)+' / '+photos.length;
  const lb = document.getElementById('lightbox');
  document.getElementById('lbToggle').textContent = lb.classList.contains('boxes-off')?'Show face boxes':'Hide face boxes';
}

function closePhoto() {
  if(_ro){try{_ro.disconnect();}catch(e){} _ro=null;}
  document.getElementById('lightbox').classList.add('hidden');
  document.getElementById('viewer').innerHTML='';
  document.body.style.overflow='';
}

document.getElementById('lbClose').onclick=closePhoto;
document.getElementById('lbPrev').onclick=()=>showPhoto(idx-1);
document.getElementById('lbNext').onclick=()=>showPhoto(idx+1);
document.getElementById('lbToggle').onclick=()=>{
  const lb=document.getElementById('lightbox');
  const off=lb.classList.toggle('boxes-off');
  document.getElementById('lbToggle').textContent=off?'Show face boxes':'Hide face boxes';
};
document.getElementById('lightbox').onclick=e=>{ if(e.target===document.getElementById('lightbox')) closePhoto(); };
document.addEventListener('keydown',e=>{
  if(document.getElementById('lightbox').classList.contains('hidden')) return;
  if(e.key==='Escape') closePhoto();
  if(e.key==='ArrowLeft') showPhoto(idx-1);
  if(e.key==='ArrowRight') showPhoto(idx+1);
});
window.addEventListener('resize',()=>{
  if(!document.getElementById('lightbox').classList.contains('hidden')){
    const img=document.getElementById('lbImg');
    positionBoxes(document.getElementById('viewer'),img);
  }
});

// ── Load data ─────────────────────────────────────────────────────────────────
(async()=>{
  try{
    const r=await fetch('/api/public/people/'+PID);
    const d=await r.json();
    if(!d.ok) throw Error(d.error||'Not found');
    const p=d.data;
    personName=p.name; photos=p.photos||[];
    document.getElementById('personName').textContent=p.name;
    document.getElementById('photoCount').textContent=photos.length+' photo'+(photos.length===1?'':'s');
    document.title=p.name+' - IJANT Gallery';
    document.getElementById('skel').classList.add('hidden');
    if(!photos.length){ document.getElementById('empty').classList.remove('hidden'); return; }
    const grid=document.getElementById('photoGrid');
    grid.innerHTML=photos.map((ph,i)=>{
      const bx=(ph.faces||[]).map(f=>
        `<div class="face-box" data-x1="${f.x1}" data-y1="${f.y1}" data-x2="${f.x2}" data-y2="${f.y2}"></div>`
      ).join('');
      return `<button class="photo-btn" data-i="${i}"><span class="frame"><img loading="lazy" src="${imgUrl(ph.id)}" alt="Photo ${i+1}">${bx}</span></button>`;
    }).join('');
    grid.classList.remove('hidden');
    grid.querySelectorAll('.photo-btn').forEach(btn=>{
      btn.onclick=()=>showPhoto(Number(btn.dataset.i));
      const frame=btn.querySelector('.frame');
      const img=frame.querySelector('img');
      const place=()=>positionBoxes(frame,img);
      if(img.complete&&img.naturalWidth>0) place();
      else img.addEventListener('load',place,{once:true});
    });
  }catch(e){
    document.getElementById('skel').classList.add('hidden');
    document.getElementById('personName').textContent='Error';
    document.getElementById('photoCount').textContent=e.message;
  }
})();
</script></body></html>"""


# ════════════════════════════════════════════════════════
#  ALL IMAGES  /images
# ════════════════════════════════════════════════════════
def _images_page():
    skels = '<div class="skel"><div class="skel-img" style="aspect-ratio:1"></div></div>' * 20
    return """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>All Images - IJANT Gallery</title>
<style>""" + _CSS + r"""
.img-layout { display:grid; grid-template-columns:1fr 300px; gap:20px; align-items:start; }
@media(max-width:900px){ .img-layout{ grid-template-columns:1fr; } .side-panel{ display:none!important; } }
.masonry { display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:10px; }
.skel-grid { grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:10px; }

/* Image card with hover popover */
.img-wrap { position:relative; }
.img-card { border:1px solid var(--line); border-radius:10px; overflow:hidden; background:#161b25;
  cursor:zoom-in; transition:transform .15s,border-color .15s; display:block; padding:0; width:100%; }
.img-card:hover { transform:translateY(-2px); border-color:var(--accent); }
.img-card img { width:100%; aspect-ratio:1; object-fit:contain; display:block; background:#10141d; }
.img-label { padding:6px 10px; font-size:12px; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.load-more { text-align:center; margin-top:30px; }
/* Thumbnail face boxes */
.thumb-fbox { position:absolute; border:1.5px solid var(--accent);
  box-shadow:0 0 0 1px rgba(0,0,0,.6),0 0 8px rgba(169,243,107,.5);
  border-radius:2px; pointer-events:none; z-index:10; display:none; }
.img-wrap.boxes-on .thumb-fbox { display:block; }
.thumb-fbox::after { content:attr(data-name); position:absolute; bottom:calc(100%+2px); left:0;
  background:rgba(10,12,16,.92); color:var(--accent); font-size:9px; font-weight:600;
  padding:1px 5px; border-radius:3px; white-space:nowrap; pointer-events:none; }
/* Grid toggle btn */
.grid-toolbar { display:flex; justify-content:flex-end; margin-bottom:10px; }
#thumbTogBtn { background:var(--card); border:1px solid var(--line); color:inherit;
  border-radius:8px; padding:6px 14px; font-size:13px; cursor:pointer; transition:all .15s; }
#thumbTogBtn:hover { border-color:var(--accent); color:var(--accent); }
#thumbTogBtn.on { border-color:var(--accent); color:var(--accent); background:rgba(169,243,107,.08); }
@media(max-width:600px){ .masonry,.skel-grid{ grid-template-columns:repeat(2,1fr); } }

/* Thumbnail hover popover */
.hover-pop {
  position:absolute; bottom:calc(100% + 8px); left:50%; transform:translateX(-50%);
  background:rgba(10,13,20,.97); border:1px solid var(--line); border-radius:10px;
  padding:10px 12px; min-width:140px; max-width:220px; z-index:60;
  box-shadow:0 8px 24px rgba(0,0,0,.6); pointer-events:none;
  opacity:0; transition:opacity .15s; white-space:nowrap;
}
.img-wrap:hover .hover-pop { opacity:1; pointer-events:auto; }
.hover-pop-title { font-size:11px; color:var(--muted); margin-bottom:6px; letter-spacing:.05em; text-transform:uppercase; }
.hover-pop-person { display:flex; align-items:center; gap:7px; padding:3px 0; font-size:13px; font-weight:500; }
.hover-pop-dot { width:7px; height:7px; border-radius:50%; background:var(--accent); flex-shrink:0; }
.hover-pop-empty { font-size:12px; color:var(--muted); }
/* arrow */
.hover-pop::after { content:''; position:absolute; top:100%; left:50%; transform:translateX(-50%);
  border:6px solid transparent; border-top-color:var(--line); }

/* Side panel */
.side-panel { position:sticky; top:80px; background:var(--card); border:1px solid var(--line);
  border-radius:16px; padding:20px; min-height:200px; }
.side-panel h3 { margin:0 0 14px; font-size:15px; color:var(--muted); font-weight:500; }
.side-person { display:flex; align-items:center; gap:10px; padding:9px 0;
  border-bottom:1px solid var(--line); color:inherit; transition:color .15s; cursor:pointer;
  background:none; border-left:none; border-right:none; border-top:none; width:100%; text-align:left; }
.side-person:last-child { border-bottom:none; }
.side-person:hover { color:var(--accent); }
.side-person img { width:38px; height:38px; border-radius:50%; object-fit:cover; background:#1c222e;
  border:2px solid var(--line); flex-shrink:0; }
.side-person .sp-name { font-size:14px; font-weight:500; }
.side-empty { color:var(--muted); font-size:14px; padding:20px 0; text-align:center; }

/* Lightbox (image viewer) */
#lb { position:fixed; inset:0; background:rgba(3,4,7,.95); backdrop-filter:blur(10px);
  z-index:100; display:flex; align-items:center; justify-content:center; padding:70px 70px 55px; user-select:none; }
#lb.hidden { display:none !important; }
#vwr { position:relative; display:inline-block; max-width:100%; max-height:100%; line-height:0; }
#vwr img { max-width:100%; max-height:calc(100vh - 130px); object-fit:contain; display:block;
  border-radius:6px; box-shadow:0 12px 40px rgba(0,0,0,.8); }
.fbox { position:absolute; border:2.5px solid var(--accent);
  box-shadow:0 0 0 1px rgba(0,0,0,.7),0 0 14px rgba(169,243,107,.6);
  border-radius:3px; cursor:pointer; z-index:50; display:none; }
.fbox.placed { display:block; }
.fbox::after { content:attr(data-name); position:absolute; bottom:calc(100%+4px); left:0;
  background:rgba(10,12,16,.92); color:var(--accent); font-size:11px; font-weight:600;
  padding:2px 8px; border-radius:4px; white-space:nowrap; border:1px solid var(--line);
  opacity:0; transition:opacity .15s; pointer-events:none; }
.fbox:hover::after { opacity:1; }
#lb.boxes-off .fbox { display:none !important; }
.lbc { position:absolute; border:1px solid var(--line); background:rgba(23,27,36,.9);
  color:white; border-radius:10px; padding:10px 16px; cursor:pointer; font-size:14px;
  font-weight:500; transition:all .15s; z-index:110; }
.lbc:hover { border-color:var(--accent); color:var(--accent); }
#lbClose { top:14px; right:14px; }
#lbTog { top:14px; right:122px; }
#lbPrev { left:14px; top:50%; transform:translateY(-50%); font-size:20px; padding:14px 18px; }
#lbNext { right:14px; top:50%; transform:translateY(-50%); font-size:20px; padding:14px 18px; }
#lbCnt { position:absolute; bottom:14px; left:50%; transform:translateX(-50%); color:#d3d7e0;
  font-size:13px; background:rgba(23,27,36,.7); padding:5px 14px; border-radius:20px;
  border:1px solid var(--line); white-space:nowrap; }
/* People panel inside lightbox */
#lbPeople { position:absolute; right:14px; top:50%; transform:translateY(-50%);
  background:rgba(13,16,24,.92); border:1px solid var(--line); border-radius:14px;
  padding:14px 16px; min-width:160px; max-width:220px; max-height:70vh; overflow-y:auto;
  z-index:115; backdrop-filter:blur(8px); }
#lbPeople:empty { display:none; }
#lbPeople:not(:empty) { display:block; }
#lbPeopleList { display:flex; flex-direction:column; gap:6px; }
.lb-person { display:flex; align-items:center; gap:8px; background:none; border:none;
  color:inherit; cursor:pointer; padding:5px 6px; border-radius:8px; width:100%;
  text-align:left; transition:background .12s; }
.lb-person:hover { background:rgba(169,243,107,.1); color:var(--accent); }
.lb-person img { width:30px; height:30px; border-radius:50%; object-fit:cover;
  border:1.5px solid var(--line); flex-shrink:0; }
.lb-person .lb-pname { font-size:13px; font-weight:500; white-space:nowrap;
  overflow:hidden; text-overflow:ellipsis; }
.lb-people-hdr { font-size:10px; text-transform:uppercase; letter-spacing:.08em;
  color:var(--muted); margin-bottom:6px; }
@media(max-width:700px){ #lbPeople{ display:none !important; } }

/* Person modal (opens when clicking side-panel person) */
#personModal { position:fixed; inset:0; background:rgba(3,4,7,.92); backdrop-filter:blur(12px);
  z-index:200; display:flex; align-items:center; justify-content:center; padding:20px; }
#personModal.hidden { display:none !important; }
#personModalBox {
  background:var(--card); border:1px solid var(--line); border-radius:20px;
  width:min(860px,100%); max-height:85vh; display:flex; flex-direction:column;
  overflow:hidden; box-shadow:0 24px 60px rgba(0,0,0,.8);
}
#personModalHead {
  display:flex; align-items:center; justify-content:space-between;
  padding:18px 22px; border-bottom:1px solid var(--line); flex-shrink:0;
}
#personModalName { font-size:20px; font-weight:700; margin:0; }
#personModalCount { color:var(--muted); font-size:14px; }
#personModalClose { background:none; border:1px solid var(--line); border-radius:8px;
  color:inherit; padding:6px 14px; cursor:pointer; font-size:13px; transition:all .15s; }
#personModalClose:hover { border-color:var(--accent); color:var(--accent); }
#personModalGrid {
  display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr));
  gap:10px; padding:18px 22px; overflow-y:auto; flex:1;
}
.pm-photo { padding:0; border:1px solid var(--line); background:#161b25; border-radius:10px;
  overflow:hidden; cursor:zoom-in; transition:transform .15s,border-color .15s; display:block; width:100%; }
.pm-photo:hover { transform:translateY(-2px); border-color:var(--accent); }
.pm-photo img { width:100%; aspect-ratio:1; object-fit:contain; display:block; background:#10141d; }
.pm-loader { padding:40px; text-align:center; color:var(--muted); font-size:14px; }

@media(max-width:600px){
  #lb{ padding:60px 6px 50px; }
  #lbPrev{ left:4px; } #lbNext{ right:4px; }
  #lbTog{ right:90px; padding:8px 12px; font-size:12px; }
  #lbClose{ padding:8px 12px; font-size:12px; }
}
</style></head><body>""" + _nav("images") + r"""
<main>
<div class="page-hdr"><h1>All Images</h1><p id="subtitle"><small>(let ijads pink computing unit process)</small></p></div>
<div id="skel" class="skel-grid" style="display:grid">""" + skels + r"""</div>
<div class="img-layout hidden" id="mainLayout">
  <div>
    <div class="grid-toolbar"><button id="thumbTogBtn">&#128100; Show face boxes</button></div>
    <div id="grid" class="masonry"></div>
    <div class="load-more"><button class="btn hidden" id="moreBtn">Load more</button></div>
  </div>
  <aside class="side-panel" id="sidePanel">
    <h3>People in image</h3>
    <div id="sideContent"><div class="side-empty">Hover or click a photo to see who's in it</div></div>
  </aside>
</div>
</main>

<!-- Image lightbox -->
<div id="lb" class="hidden" role="dialog" aria-modal="true">
  <button class="lbc" id="lbClose">&#10005; Close</button>
  <button class="lbc" id="lbTog">Hide face boxes</button>
  <button class="lbc" id="lbPrev">&#8592;</button>
  <div id="vwr"></div>
  <button class="lbc" id="lbNext">&#8594;</button>
  <div id="lbCnt"></div>
  <div id="lbPeople">
    <div id="lbPeopleList"></div>
  </div>
</div>

<!-- Person modal -->
<div id="personModal" class="hidden" role="dialog" aria-modal="true" aria-label="Person gallery">
  <div id="personModalBox">
    <div id="personModalHead">
      <div>
        <div id="personModalName">Loading...</div>
        <div id="personModalCount" class="muted"></div>
      </div>
      <button id="personModalClose">&#10005; Close</button>
    </div>
    <div id="personModalGrid"></div>
  </div>
</div>

<script>""" + _JS + r"""
const PAGE=60; let all=[], offset=0, curIdx=0, roObj=null;

// ── Face box positioning ──────────────────────────────────────────────────────
function posBoxes(container, img) {
  if(!container||!img||!img.naturalWidth||!img.naturalHeight) return;
  const iw=img.clientWidth, ih=img.clientHeight;
  if(!iw||!ih){ requestAnimationFrame(()=>posBoxes(container,img)); return; }
  const scale=Math.min(iw/img.naturalWidth, ih/img.naturalHeight);
  const rw=img.naturalWidth*scale, rh=img.naturalHeight*scale;
  const ox=(iw-rw)/2, oy=(ih-rh)/2;
  container.querySelectorAll('.fbox').forEach(b=>{
    const x1=parseFloat(b.dataset.x1)||0, y1=parseFloat(b.dataset.y1)||0;
    const x2=parseFloat(b.dataset.x2)||0, y2=parseFloat(b.dataset.y2)||0;
    b.style.left=(ox+x1*scale)+'px'; b.style.top=(oy+y1*scale)+'px';
    b.style.width=Math.max(2,(x2-x1)*scale)+'px'; b.style.height=Math.max(2,(y2-y1)*scale)+'px';
    b.classList.add('placed');
  });
}

// ── Face-crop avatar helper ─────────────────────────────────────────────────
// Renders a circular avatar that zooms into the detected face region.
// p = person object with cover_photo_id, face_x1/y1/x2/y2
// size = pixel diameter of the circle
function faceCropAvatar(p, size) {
  if (!p.cover_photo_id) {
    // Fallback: letter initial
    return `<span style="width:${size}px;height:${size}px;border-radius:50%;background:#1c222e;border:2px solid var(--line);display:inline-flex;align-items:center;justify-content:center;font-size:${Math.round(size*0.38)}px;font-weight:700;color:var(--accent);flex-shrink:0">${esc((p.name||'?')[0].toUpperCase())}</span>`;
  }
  const hasFace = p.face_x1 != null && p.face_x2 != null && p.face_x2 > p.face_x1;
  // Unique id so we can find the img after injection
  const uid = 'fca-' + p.id + '-' + p.cover_photo_id;
  // We will apply transform after load via a global handler
  const dataAttrs = hasFace
    ? `data-fx1="${p.face_x1}" data-fy1="${p.face_y1}" data-fx2="${p.face_x2}" data-fy2="${p.face_y2}"`
    : '';
  return `<span id="${uid}" style="width:${size}px;height:${size}px;border-radius:50%;overflow:hidden;display:inline-flex;align-items:center;justify-content:center;flex-shrink:0;border:2px solid var(--line);background:#111;position:relative">
    <img id="${uid}-img" src="${imgUrl(p.cover_photo_id)}" alt="${esc(p.name)}" ${dataAttrs}
      style="width:100%;height:100%;object-fit:cover;transition:all .15s"
      onload="applyFaceCrop(this)">
  </span>`;
}

function applyFaceCrop(img) {
  const x1 = parseFloat(img.dataset.fx1);
  const y1 = parseFloat(img.dataset.fy1);
  const x2 = parseFloat(img.dataset.fx2);
  const y2 = parseFloat(img.dataset.fy2);
  if (isNaN(x1) || isNaN(y1) || isNaN(x2) || isNaN(y2) || x2 <= x1 || y2 <= y1) return;

  const nw = img.naturalWidth, nh = img.naturalHeight;
  if (!nw || !nh) return;

  const container = img.parentElement;
  const cw = container.offsetWidth  || 36;
  const ch = container.offsetHeight || 36;

  // Face dimensions in original pixels
  const fw = x2 - x1, fh = y2 - y1;
  // Scale image so the face region (+ 40% padding) fills the avatar circle
  const pad = 1.4;
  const scale = Math.min(cw / (fw * pad), ch / (fh * pad));

  // Face center in scaled-image space
  const fcx_scaled = ((x1 + x2) / 2) * scale;
  const fcy_scaled = ((y1 + y2) / 2) * scale;

  // Offset so face center lands at avatar center
  const left = cw / 2 - fcx_scaled;
  const top  = ch / 2 - fcy_scaled;

  img.style.position  = 'absolute';
  img.style.objectFit = 'unset';
  img.style.width     = (nw * scale) + 'px';
  img.style.height    = (nh * scale) + 'px';
  img.style.left      = left + 'px';
  img.style.top       = top  + 'px';
}

// ── Side panel + lightbox people panel ─────────────────────────────────────
function updateSide(imgObj) {
  const ppl = imgObj ? (imgObj.people || []) : [];
  const sc = document.getElementById('sideContent');
  const lbList = document.getElementById('lbPeopleList');

  if(!ppl.length){
    sc.innerHTML='<div class="side-empty">No tagged people</div>';
    lbList.innerHTML='';
    return;
  }

  const personBtns = ppl.map(p=>`
    <button class="side-person" data-pid="${p.id}" data-name="${esc(p.name)}">
      ${faceCropAvatar(p, 36)}
      <span class="sp-name">${esc(p.name)}</span>
    </button>`);

  sc.innerHTML = personBtns.join('');
  sc.querySelectorAll('.side-person').forEach(btn=>{
    btn.onclick = () => openPersonModal(Number(btn.dataset.pid), btn.dataset.name);
  });

  // Lightbox people panel
  lbList.innerHTML = '<div class="lb-people-hdr">People in photo</div>' +
    ppl.map(p=>`
      <button class="lb-person" data-pid="${p.id}" data-name="${esc(p.name)}">
        ${faceCropAvatar(p, 38)}
        <span class="lb-pname">${esc(p.name)}</span>
      </button>`).join('');
  lbList.querySelectorAll('.lb-person').forEach(btn=>{
    btn.onclick = () => openPersonModal(Number(btn.dataset.pid), btn.dataset.name);
  });
}

// ── Person Modal ──────────────────────────────────────────────────────────────
async function openPersonModal(pid, name) {
  const modal = document.getElementById('personModal');
  document.getElementById('personModalName').textContent = name;
  document.getElementById('personModalCount').textContent = '';
  document.getElementById('personModalGrid').innerHTML = '<div class="pm-loader">(let ijads pink computing unit process)</div>';
  modal.classList.remove('hidden');
  document.body.style.overflow = 'hidden';

  try {
    const r = await fetch('/api/public/people/' + pid);
    const d = await r.json();
    if(!d.ok) throw Error(d.error);
    const p = d.data;
    document.getElementById('personModalName').textContent = p.name;
    document.getElementById('personModalCount').textContent = p.photos.length + ' photo' + (p.photos.length===1?'':'s');
    if(!p.photos.length){
      document.getElementById('personModalGrid').innerHTML = '<div class="pm-loader">No photos.</div>';
      return;
    }
    document.getElementById('personModalGrid').innerHTML = p.photos.map((ph,i)=>`
      <button class="pm-photo" data-i="${i}" data-pid="${pid}">
        <img loading="lazy" src="${imgUrl(ph.id)}" alt="Photo ${i+1}">
      </button>`).join('');
    // Clicking a photo in the person modal closes modal and opens full lightbox
    document.getElementById('personModalGrid').querySelectorAll('.pm-photo').forEach(btn=>{
      btn.onclick = () => {
        closePersonModal();
        // find this photo index in all[] by image id
        const ph = p.photos[Number(btn.dataset.i)];
        const globalIdx = all.findIndex(a=>a.id===ph.id);
        if(globalIdx >= 0) showPhoto(globalIdx);
      };
    });
  } catch(e) {
    document.getElementById('personModalGrid').innerHTML = '<div class="pm-loader">Error: '+esc(e.message)+'</div>';
  }
}

function closePersonModal() {
  document.getElementById('personModal').classList.add('hidden');
  document.body.style.overflow = '';
}

document.getElementById('personModalClose').onclick = closePersonModal;
document.getElementById('personModal').onclick = e => {
  if(e.target === document.getElementById('personModal')) closePersonModal();
};

// ── Image lightbox ────────────────────────────────────────────────────────────
function showPhoto(i) {
  if(!all.length) return;
  curIdx = ((i%all.length)+all.length)%all.length;
  const ph = all[curIdx];
  const boxes = (ph.faces||[]).map(f=>
    `<div class="fbox" data-x1="${f.x1}" data-y1="${f.y1}" data-x2="${f.x2}" data-y2="${f.y2}" data-name="${esc(f.name||'')}"></div>`
  ).join('');
  document.getElementById('vwr').innerHTML=`<img id="lbImg" src="${imgUrl(ph.id)}" alt="${esc(ph.original_name||'')}">${boxes}`;
  // Show FIRST so dimensions work
  document.getElementById('lb').classList.remove('hidden');
  document.body.style.overflow='hidden';
  const img=document.getElementById('lbImg');
  const vwr=document.getElementById('vwr');
  if(roObj){try{roObj.disconnect();}catch(e){} roObj=null;}
  const place=()=>{ posBoxes(vwr,img); requestAnimationFrame(()=>posBoxes(vwr,img)); };
  if(img.complete&&img.naturalWidth>0) place();
  else img.addEventListener('load',place,{once:true});
  try{ roObj=new ResizeObserver(()=>posBoxes(vwr,img)); roObj.observe(img); }catch(e){}
  document.getElementById('lbCnt').textContent=(curIdx+1)+' / '+all.length;
  document.getElementById('lbTog').textContent=document.getElementById('lb').classList.contains('boxes-off')?'Show face boxes':'Hide face boxes';
  updateSide(ph);
}

function closePhoto(){
  if(roObj){try{roObj.disconnect();}catch(e){} roObj=null;}
  document.getElementById('lb').classList.add('hidden');
  document.getElementById('vwr').innerHTML='';
  document.body.style.overflow='';
}

document.getElementById('lbClose').onclick=closePhoto;
document.getElementById('lbPrev').onclick=()=>showPhoto(curIdx-1);
document.getElementById('lbNext').onclick=()=>showPhoto(curIdx+1);
document.getElementById('lbTog').onclick=()=>{
  const off=document.getElementById('lb').classList.toggle('boxes-off');
  document.getElementById('lbTog').textContent=off?'Show face boxes':'Hide face boxes';
};
document.getElementById('lb').onclick=e=>{ if(e.target===document.getElementById('lb')) closePhoto(); };
document.addEventListener('keydown',e=>{
  const lbHid = document.getElementById('lb').classList.contains('hidden');
  const pmHid = document.getElementById('personModal').classList.contains('hidden');
  if(!pmHid && e.key==='Escape'){ closePersonModal(); return; }
  if(!lbHid){
    if(e.key==='Escape') closePhoto();
    if(e.key==='ArrowLeft') showPhoto(curIdx-1);
    if(e.key==='ArrowRight') showPhoto(curIdx+1);
  }
});
window.addEventListener('resize',()=>{
  if(!document.getElementById('lb').classList.contains('hidden'))
    posBoxes(document.getElementById('vwr'),document.getElementById('lbImg'));
});

// ── Grid rendering ────────────────────────────────────────────────────────────
function buildHoverPop(imgObj) {
  const ppl = imgObj.people || [];
  if(!ppl.length) return '<div class="hover-pop"><div class="hover-pop-empty">No tags</div></div>';
  const items = ppl.map(p=>`
    <div class="hover-pop-person">
      <div class="hover-pop-dot"></div>
      <span>${esc(p.name)}</span>
    </div>`).join('');
  return `<div class="hover-pop"><div class="hover-pop-title">People</div>${items}</div>`;
}

let thumbBoxesOn = false;
function applyThumbBoxState() {
  document.querySelectorAll('.img-wrap').forEach(w => w.classList.toggle('boxes-on', thumbBoxesOn));
  const btn = document.getElementById('thumbTogBtn');
  btn.innerHTML = thumbBoxesOn ? '&#128100; Hide face boxes' : '&#128100; Show face boxes';
  btn.classList.toggle('on', thumbBoxesOn);
}
document.getElementById('thumbTogBtn').onclick = () => { thumbBoxesOn = !thumbBoxesOn; applyThumbBoxState(); };

function buildThumbBoxes(imgObj, cardEl) {
  const faces = imgObj.faces || [];
  if(!faces.length) return;
  const img = cardEl.querySelector('img');
  const doPlace = () => {
    if(!img.naturalWidth || !img.clientWidth) return;
    const iw = img.clientWidth, ih = img.clientHeight;
    const scale = Math.min(iw/img.naturalWidth, ih/img.naturalHeight);
    const rw = img.naturalWidth*scale, rh = img.naturalHeight*scale;
    const ox = (iw-rw)/2, oy = (ih-rh)/2;
    const wrapRect = cardEl.parentElement.getBoundingClientRect();
    const imgRect  = img.getBoundingClientRect();
    const relTop   = imgRect.top  - wrapRect.top;
    const relLeft  = imgRect.left - wrapRect.left;
    faces.forEach((f, fi) => {
      let box = cardEl.parentElement.querySelector(`.thumb-fbox[data-fi="${fi}"]`);
      if(!box){ box = document.createElement('div'); box.className='thumb-fbox'; box.dataset.fi=fi; box.dataset.name=f.name||''; cardEl.parentElement.appendChild(box); }
      box.style.left   = (relLeft + ox + f.x1*scale)+'px';
      box.style.top    = (relTop  + oy + f.y1*scale)+'px';
      box.style.width  = Math.max(2,(f.x2-f.x1)*scale)+'px';
      box.style.height = Math.max(2,(f.y2-f.y1)*scale)+'px';
    });
  };
  if(img.complete && img.naturalWidth > 0) doPlace();
  else img.addEventListener('load', doPlace, {once:true});
  try{ new ResizeObserver(doPlace).observe(img); }catch(e){}
}

function batch(){
  const g=document.getElementById('grid');
  all.slice(offset,offset+PAGE).forEach((imgObj,rel)=>{
    const i=offset+rel;
    const wrap = document.createElement('div');
    wrap.className = 'img-wrap' + (thumbBoxesOn ? ' boxes-on' : '');
    wrap.innerHTML = buildHoverPop(imgObj);
    const b=document.createElement('button');
    b.className='img-card';
    b.innerHTML=`<img loading="lazy" src="${imgUrl(imgObj.id)}" alt="${esc(imgObj.original_name||'')}">
      <div class="img-label">${esc(imgObj.original_name||'Photo')}</div>`;
    b.onclick=()=>showPhoto(i);
    wrap.addEventListener('mouseenter',()=>updateSide(imgObj));
    wrap.insertBefore(b, wrap.firstChild);
    g.appendChild(wrap);
    buildThumbBoxes(imgObj, b);
  });
  offset=Math.min(offset+PAGE,all.length);
  document.getElementById('moreBtn').classList.toggle('hidden',offset>=all.length);
}

document.getElementById('moreBtn').onclick=batch;

(async()=>{
  try{
    const r=await fetch('/api/images'); const d=await r.json();
    if(!d.ok) throw Error(d.error);
    all=d.data;
    document.getElementById('subtitle').textContent=all.length+' photos in the archive';
    document.getElementById('skel').classList.add('hidden');
    document.getElementById('mainLayout').classList.remove('hidden');
    batch();
  }catch(e){
    document.getElementById('skel').classList.add('hidden');
    document.getElementById('subtitle').textContent='Failed: '+e.message;
  }
})();
</script></body></html>"""


# ════════════════════════════════════════════════════════════════════════════════
#  FLASK ROUTES - HTML Pages
# ════════════════════════════════════════════════════════════════════════════════

@app.get("/")
def home():
    return _home_page()

@app.get("/people")
def people_page():
    return _people_page()

@app.get("/people/<int:pid>")
def person_page(pid: int):
    return _person_page()

@app.get("/images")
def images_page():
    return _images_page()


# ════════════════════════════════════════════════════════════════════════════════
#  FLASK ROUTES - JSON API
# ════════════════════════════════════════════════════════════════════════════════

@app.get("/api/stats")
def api_stats():
    conn = get_db_conn()
    if conn is not None:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM people"); people = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM images"); images = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM face_boxes"); face_boxes = cur.fetchone()[0]
            return jsonify(ok=True, data={"people": people, "images": images, "face_boxes": face_boxes})
        except Exception as exc:
            app.logger.exception(exc)
        finally:
            put_db_conn(conn)
    return jsonify(ok=False, error="Database unavailable"), 503


@app.get("/api/public/people")
@app.get("/api/people")
def list_people():
    conn = get_db_conn()
    if conn is not None:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT p.person_id AS id, p.name,
                           COUNT(pi.image_id) AS photo_count,
                           MIN(pi.image_id) AS cover_photo_id
                    FROM people p
                    LEFT JOIN person_images pi ON pi.person_id = p.person_id
                    GROUP BY p.person_id, p.name
                    ORDER BY lower(p.name), p.person_id
                """)
                rows = cur.fetchall()
                return jsonify(ok=True, data=[dict(r) for r in rows])
        except Exception as exc:
            app.logger.exception(exc)
        finally:
            put_db_conn(conn)

    if SQLITE_FALLBACK.is_file():
        import sqlite3
        con = sqlite3.connect(f"file:{SQLITE_FALLBACK.as_posix()}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute("""
                SELECT p.person_id AS id, p.name,
                       COUNT(pi.image_id) AS photo_count,
                       MIN(pi.image_id) AS cover_photo_id
                FROM people p
                LEFT JOIN person_images pi ON pi.person_id = p.person_id
                GROUP BY p.person_id, p.name
                ORDER BY p.name COLLATE NOCASE, p.person_id
            """).fetchall()
            return jsonify(ok=True, data=[dict(r) for r in rows])
        finally:
            con.close()

    abort(503, description="Database not available.")


@app.get("/api/public/people/<int:pid>")
@app.get("/api/people/<int:pid>")
def get_person(pid: int):
    conn = get_db_conn()
    if conn is not None:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT person_id AS id, name FROM people WHERE person_id = %s", (pid,))
                p = cur.fetchone()
                if not p:
                    abort(404, description="Person not found")

                cur.execute("""
                    SELECT i.image_id AS id, i.original_name, i.object_key
                    FROM images i
                    JOIN person_images pi ON pi.image_id = i.image_id
                    WHERE pi.person_id = %s
                    ORDER BY i.image_id
                """, (pid,))
                image_rows = cur.fetchall()

                for r in image_rows:
                    if r.get("object_key"):
                        _IMAGE_KEY_CACHE[r["id"]] = r["object_key"]

                cur.execute("""
                    SELECT image_id, x1, y1, x2, y2, quality, det_conf
                    FROM face_boxes
                    WHERE person_id = %s
                    ORDER BY id
                """, (pid,))
                box_rows = cur.fetchall()

                boxes_by_image: Dict[int, List[Dict[str, Any]]] = {}
                for b in box_rows:
                    boxes_by_image.setdefault(b["image_id"], []).append({
                        "x1": float(b["x1"]),
                        "y1": float(b["y1"]),
                        "x2": float(b["x2"]),
                        "y2": float(b["y2"]),
                        "quality": float(b.get("quality", 0) or 0),
                        "det_conf": float(b.get("det_conf", 0) or 0),
                    })

                photos = [
                    {"id": r["id"], "original_name": r.get("original_name", ""),
                     "faces": boxes_by_image.get(r["id"], [])}
                    for r in image_rows
                ]
                return jsonify(ok=True, data={"id": p["id"], "name": p["name"], "photos": photos})
        except Exception as exc:
            app.logger.exception(exc)
        finally:
            put_db_conn(conn)

    if SQLITE_FALLBACK.is_file():
        import sqlite3
        con = sqlite3.connect(f"file:{SQLITE_FALLBACK.as_posix()}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            p = con.execute("SELECT person_id AS id, name FROM people WHERE person_id = ?", (pid,)).fetchone()
            if not p:
                abort(404, description="Person not found")
            photos = []
            for r in con.execute(
                "SELECT i.image_id AS id, i.object_key FROM images i "
                "JOIN person_images pi ON pi.image_id = i.image_id "
                "WHERE pi.person_id = ? ORDER BY i.image_id", (pid,)
            ):
                _IMAGE_KEY_CACHE[r["id"]] = r["object_key"]
                faces = con.execute(
                    "SELECT x1,y1,x2,y2,quality,det_conf FROM face_boxes WHERE person_id=? AND image_id=?",
                    (pid, r["id"])
                ).fetchall()
                photos.append({"id": r["id"], "faces": [dict(f) for f in faces]})
            return jsonify(ok=True, data={"id": p["id"], "name": p["name"], "photos": photos})
        finally:
            con.close()

    abort(503, description="Database not available.")


@app.get("/api/images")
def list_images():
    """Return all images with their face boxes and associated people names."""
    conn = get_db_conn()
    if conn is not None:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT image_id AS id, original_name, object_key FROM images ORDER BY image_id")
                rows = cur.fetchall()
                for r in rows:
                    if r.get("object_key"):
                        _IMAGE_KEY_CACHE[r["id"]] = r["object_key"]
                image_ids = [r["id"] for r in rows]
                if not image_ids:
                    return jsonify(ok=True, data=[])

                # Fetch all face boxes for all images in one query
                cur.execute("""
                    SELECT fb.image_id, fb.x1, fb.y1, fb.x2, fb.y2, p.name
                    FROM face_boxes fb
                    JOIN people p ON p.person_id = fb.person_id
                    ORDER BY fb.image_id
                """)
                box_rows = cur.fetchall()
                boxes_by_image: Dict[int, List[Dict]] = {}
                for b in box_rows:
                    iid = b["image_id"]
                    boxes_by_image.setdefault(iid, []).append({
                        "x1": float(b["x1"]), "y1": float(b["y1"]),
                        "x2": float(b["x2"]), "y2": float(b["y2"]),
                        "name": b["name"],
                    })

                # Fetch people per image: cover photo + face box coords in that cover photo
                cur.execute("""
                    SELECT pi.image_id, p.person_id AS id, p.name,
                           cov.cover_photo_id,
                           fb.x1 AS face_x1, fb.y1 AS face_y1,
                           fb.x2 AS face_x2, fb.y2 AS face_y2
                    FROM person_images pi
                    JOIN people p ON p.person_id = pi.person_id
                    JOIN (
                        SELECT person_id, MIN(image_id) AS cover_photo_id
                        FROM person_images
                        GROUP BY person_id
                    ) cov ON cov.person_id = p.person_id
                    LEFT JOIN face_boxes fb
                        ON fb.person_id = p.person_id
                        AND fb.image_id = cov.cover_photo_id
                    ORDER BY pi.image_id, p.name
                """)
                people_by_image: Dict[int, List[Dict]] = {}
                for row in cur.fetchall():
                    iid = row["image_id"]
                    people_by_image.setdefault(iid, []).append({
                        "id": row["id"], "name": row["name"],
                        "cover_photo_id": row["cover_photo_id"],
                        "face_x1": float(row["face_x1"]) if row["face_x1"] is not None else None,
                        "face_y1": float(row["face_y1"]) if row["face_y1"] is not None else None,
                        "face_x2": float(row["face_x2"]) if row["face_x2"] is not None else None,
                        "face_y2": float(row["face_y2"]) if row["face_y2"] is not None else None,
                    })

                data = [
                    {
                        "id": r["id"],
                        "original_name": r.get("original_name", ""),
                        "faces": boxes_by_image.get(r["id"], []),
                        "people": people_by_image.get(r["id"], []),
                    }
                    for r in rows
                ]
                return jsonify(ok=True, data=data)
        except Exception as exc:
            app.logger.exception(exc)
        finally:
            put_db_conn(conn)
    abort(503, description="Database not available.")


@app.get("/photo/<int:image_id>")
@app.get("/public-photo/<int:image_id>")
def get_photo(image_id: int):
    now = time.time()
    cached = _PRESIGNED_CACHE.get(image_id)
    if cached and cached[1] > now:
        return redirect(cached[0], code=302)

    key = _IMAGE_KEY_CACHE.get(image_id)
    if not key:
        conn = get_db_conn()
        if conn is not None:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT object_key FROM images WHERE image_id = %s", (image_id,))
                    row = cur.fetchone()
                    if row:
                        key = row[0]
                        _IMAGE_KEY_CACHE[image_id] = key
            finally:
                put_db_conn(conn)

    if not key and SQLITE_FALLBACK.is_file():
        import sqlite3
        con = sqlite3.connect(f"file:{SQLITE_FALLBACK.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute("SELECT object_key FROM images WHERE image_id = ?", (image_id,)).fetchone()
            if row:
                key = row[0]
                _IMAGE_KEY_CACHE[image_id] = key
        finally:
            con.close()

    if not key:
        abort(404, description="Photo not found")

    try:
        url = get_s3_client().generate_presigned_url(
            "get_object",
            Params={"Bucket": B2_BUCKET, "Key": key},
            ExpiresIn=PRESIGNED_TTL,
        )
        _PRESIGNED_CACHE[image_id] = (url, now + 1800)
        return redirect(url, code=302)
    except Exception as exc:
        app.logger.exception(f"Could not generate presigned URL for {key}: {exc}")
        abort(502, description="Could not fetch image from B2.")


@app.post("/api/refresh")
def refresh():
    _IMAGE_KEY_CACHE.clear()
    _PRESIGNED_CACHE.clear()
    conn = get_db_conn()
    if conn is not None:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM people"); pc = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM images"); ic = cur.fetchone()[0]
            return jsonify(ok=True, data={"people": pc, "images": ic, "db": "neon"})
        finally:
            put_db_conn(conn)
    return jsonify(ok=False, error="Could not reach Neon database"), 503


if __name__ == "__main__":
    print("=" * 60)
    print("IJANT Public Gallery Server - Multi-Page Edition")
    print(f"Neon Database: {'Configured' if NEON_DB_URL else 'NOT CONFIGURED'}")
    print(f"B2 Bucket:     {B2_BUCKET} @ {B2_ENDPOINT_URL}")
    print(f"Routes:        /  /people  /people/<id>  /images")
    print(f"Server URL:    http://{HOST}:{PORT}")
    print("=" * 60)
    app.run(host=HOST, port=PORT, debug=False)
