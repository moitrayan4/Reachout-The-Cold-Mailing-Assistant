"""Visual design system for the control panel.

A dark "aurora glass" theme: animated gradient background, glassmorphism cards,
gradient accents, custom fonts and styled Streamlit widgets. Everything here is
pure presentation — it renders HTML/CSS and never touches app data.
"""

from __future__ import annotations

import html
import json

import streamlit as st
import streamlit.components.v1 as components

# ---------------------------------------------------------------------------
# Global stylesheet
# ---------------------------------------------------------------------------

_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap');

:root {
  --bg-0: #07081a;
  --bg-1: #0d1030;
  --ink: #eef0ff;
  --muted: #9aa0c7;
  --line: rgba(255,255,255,0.08);
  --glass: rgba(255,255,255,0.045);
  --glass-2: rgba(255,255,255,0.07);
  --grad: linear-gradient(120deg,#7c5cff 0%,#b15cff 45%,#2dd4ff 100%);
  --grad-warm: linear-gradient(120deg,#ff7b6b 0%,#ffb15c 100%);
  --grad-green: linear-gradient(120deg,#34e0a1 0%,#2dd4ff 100%);
  --radius: 18px;
  --shadow: 0 18px 45px -20px rgba(0,0,0,0.8);
}

/* ---- Background: deep space with drifting aurora blobs ---- */
.stApp {
  background:
    radial-gradient(900px 600px at 12% -8%, rgba(124,92,255,0.22), transparent 60%),
    radial-gradient(800px 600px at 105% 8%, rgba(45,212,255,0.16), transparent 55%),
    radial-gradient(700px 700px at 50% 120%, rgba(177,92,255,0.16), transparent 60%),
    linear-gradient(180deg, var(--bg-0), var(--bg-1));
  background-attachment: fixed;
  color: var(--ink);
  font-family: 'Inter', system-ui, sans-serif;
}

.block-container { padding-top: 2.2rem; padding-bottom: 4rem; max-width: 1280px; }

/* ---- Floating 3D mail icons (positions driven by JS physics) ---- */
.mail-field {
  position: fixed; inset: 0; z-index: 0;
  pointer-events: none; overflow: hidden;
}
.mail-field .mail3d {
  position: absolute; top: 0; left: 0;
  display: block; will-change: transform;
  filter: drop-shadow(0 10px 16px rgba(6,8,26,0.45));
}
.mail-field .mail3d svg { display: block; width: 100%; height: 100%; }

/* Keep all real content above the floating field */
[data-testid="stHeader"] { background: transparent; }
.block-container { position: relative; z-index: 1; }

h1,h2,h3,h4 { font-family: 'Space Grotesk','Inter',sans-serif !important; letter-spacing:-0.01em; }
[data-testid="stMarkdownContainer"] p { color: var(--ink); }
small, .stCaption, [data-testid="stCaptionContainer"] { color: var(--muted) !important; }

/* ---- Hero ---- */
.hero {
  position: relative; overflow: hidden;
  border:1px solid var(--line); border-radius: 24px;
  background: var(--glass);
  backdrop-filter: blur(14px);
  padding: 26px 30px; margin-bottom: 22px;
  box-shadow: var(--shadow);
}
.hero::before {
  content:""; position:absolute; inset:-2px;
  background: var(--grad); opacity:0.10; filter: blur(20px); z-index:0;
}
.hero > * { position: relative; z-index: 1; }
.hero h1 {
  margin:0; font-size: 2.05rem; font-weight:700;
  background: linear-gradient(120deg,#fff, #c9c2ff 60%, #8fe9ff);
  -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent;
}
.hero p { margin:6px 0 0; color: var(--muted); font-size:0.98rem; }
.hero .pills { margin-top:14px; display:flex; gap:8px; flex-wrap:wrap; }
.pill {
  font-size:0.74rem; font-weight:600; letter-spacing:.02em;
  padding:5px 11px; border-radius:999px; border:1px solid var(--line);
  background: var(--glass-2); color: var(--ink);
  display:inline-flex; align-items:center; gap:7px;
}
.pill .dot { width:7px; height:7px; border-radius:50%; box-shadow:0 0 10px currentColor; }
.dot-on { background:#34e0a1; color:#34e0a1; }
.dot-off { background:#6b7099; color:#6b7099; box-shadow:none; }

/* ---- Metric cards ---- */
.metric-grid { display:grid; grid-template-columns: repeat(4,1fr); gap:14px; margin: 6px 0 4px; }
.m-card {
  border:1px solid var(--line); border-radius: var(--radius);
  background: var(--glass); backdrop-filter: blur(10px);
  padding:16px 18px; position:relative; overflow:hidden;
  transition: transform .18s ease, border-color .18s ease, box-shadow .18s ease;
}
.m-card:hover { transform: translateY(-3px); border-color: rgba(124,92,255,0.55); box-shadow: var(--shadow); }
.m-card::after {
  content:""; position:absolute; left:0; top:0; height:100%; width:4px; background: var(--grad);
}
.m-card .m-label { color:var(--muted); font-size:0.78rem; font-weight:600; text-transform:uppercase; letter-spacing:.06em; }
.m-card .m-value {
  font-family:'Space Grotesk',sans-serif; font-weight:700; font-size:2.0rem; line-height:1.1; margin-top:6px;
  background: var(--grad); -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent;
}
.m-card.warm::after { background: var(--grad-warm); }
.m-card.warm .m-value { background: var(--grad-warm); -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent; }
.m-card.green::after { background: var(--grad-green); }
.m-card.green .m-value { background: var(--grad-green); -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent; }
@media (max-width: 1100px){ .metric-grid{ grid-template-columns: repeat(2,1fr);} }

.chip {
  display:inline-block; font-size:0.72rem; font-weight:600; padding:3px 9px; border-radius:999px;
  background: var(--glass-2); border:1px solid var(--line); color:var(--muted); margin-right:6px;
}

/* ---- Tabs: pill style ---- */
[data-testid="stTabs"] [data-baseweb="tab-list"] {
  gap:6px; background: var(--glass); padding:6px; border-radius:14px; border:1px solid var(--line);
}
[data-testid="stTabs"] [data-baseweb="tab"] {
  height:38px; border-radius:10px; color:var(--muted); font-weight:600; padding:0 16px;
  background: transparent; border:none;
}
[data-testid="stTabs"] [data-baseweb="tab"]:hover { color:var(--ink); background: var(--glass-2); }
[data-testid="stTabs"] [aria-selected="true"] {
  color:#fff !important; background: var(--grad) !important; box-shadow: 0 8px 20px -8px rgba(124,92,255,0.8);
}
[data-testid="stTabs"] [data-baseweb="tab-highlight"],
[data-testid="stTabs"] [data-baseweb="tab-border"] { display:none; }

/* ---- Buttons ---- */
.stButton > button, .stFormSubmitButton > button {
  border-radius:12px; font-weight:600; border:1px solid var(--line);
  background: var(--glass-2); color:var(--ink); transition: all .16s ease; padding:8px 16px;
}
.stButton > button:hover, .stFormSubmitButton > button:hover {
  border-color: rgba(124,92,255,0.6); transform: translateY(-1px); color:#fff;
}
.stButton > button[kind="primary"], .stFormSubmitButton > button {
  background: var(--grad); border:none; color:#fff;
  box-shadow: 0 10px 24px -10px rgba(124,92,255,0.85);
}
.stButton > button[kind="primary"]:hover { filter: brightness(1.08); transform: translateY(-2px); }

/* ---- Expanders as glass cards ---- */
[data-testid="stExpander"] {
  border:1px solid var(--line) !important; border-radius:16px !important;
  background: var(--glass) !important; backdrop-filter: blur(8px);
  margin-bottom:12px; overflow:hidden; transition: border-color .16s ease, transform .16s ease;
}
[data-testid="stExpander"]:hover { border-color: rgba(124,92,255,0.45) !important; transform: translateY(-2px); }
[data-testid="stExpander"] summary { padding:14px 18px; font-weight:600; }
[data-testid="stExpander"] summary:hover { color:#fff; }

/* ---- Dataframes ---- */
[data-testid="stDataFrame"] { border:1px solid var(--line); border-radius:14px; overflow:hidden; }

/* ---- Inputs ---- */
[data-baseweb="input"], [data-baseweb="textarea"], .stTextInput input, .stTextArea textarea {
  border-radius:12px !important; background: var(--glass) !important; color:var(--ink) !important;
}

/* ---- Sidebar (removed — hide it and its expand control entirely) ---- */
[data-testid="stSidebar"],
[data-testid="stSidebarCollapsedControl"],
[data-testid="collapsedControl"] { display:none !important; }

/* ---- Boot / cold-start overlay (centered) ---- */
.boot-overlay {
  position: fixed; inset: 0; z-index: 9999;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 22px;
  background: radial-gradient(1200px 600px at 50% 40%, rgba(124,92,255,0.10), transparent 70%),
              linear-gradient(180deg, #0b0d1a, #07081a);
}
.boot-overlay .boot-spinner {
  width: 64px; height: 64px; border-radius: 50%;
  border: 4px solid rgba(255,255,255,0.10);
  border-top-color: #7c5cff;
  border-right-color: #36d6c3;
  animation: boot-spin 0.9s linear infinite;
}
.boot-overlay .boot-text {
  font-family: 'Space Grotesk', sans-serif; font-size: 1.5rem; font-weight: 600;
  letter-spacing: 0.5px;
  background: var(--grad); -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent;
}
@keyframes boot-spin { to { transform: rotate(360deg); } }

/* ---- Streamlit metric (used in sidebar) ---- */
[data-testid="stMetric"] {
  background: var(--glass); border:1px solid var(--line); border-radius:14px; padding:12px 14px;
}
[data-testid="stMetricValue"] { font-family:'Space Grotesk',sans-serif; }

/* ---- Section heading ---- */
.section { display:flex; align-items:center; gap:10px; margin: 6px 0 14px; }
.section .bar { width:4px; height:22px; border-radius:4px; background: var(--grad); }
.section h3 { margin:0; font-size:1.18rem; }

/* ---- Alerts a touch softer ---- */
[data-testid="stAlert"] { border-radius:14px; border:1px solid var(--line); }

/* ---- Scrollbar ---- */
::-webkit-scrollbar { width:10px; height:10px; }
::-webkit-scrollbar-thumb { background: rgba(124,92,255,0.4); border-radius:10px; }
::-webkit-scrollbar-track { background: transparent; }

#MainMenu, footer, [data-testid="stToolbar"] { visibility:hidden; }

/* ---- code/log box ---- */
.stCode, pre { border-radius:14px !important; border:1px solid var(--line); }
</style>
"""


# Color schemes for the 3D envelopes, aligned to the theme accents.
# (body_light, body_dark, flap_light, flap_dark, edge_highlight)
_MAIL_SCHEMES = (
    ("#c2b2ff", "#7c5cff", "#a489ff", "#5a3fd0", "#efeaff"),  # purple
    ("#e7b6ff", "#b15cff", "#cf8cff", "#8a3fd6", "#f8edff"),  # magenta
    ("#b3edff", "#2dd4ff", "#7fdcff", "#159fd0", "#e7faff"),  # cyan
    ("#d4ccff", "#9b8cff", "#bdb1ff", "#6f5fd6", "#f1eeff"),  # lavender
)


def _mail_svg(i: int, scheme: tuple[str, str, str, str, str]) -> str:
    """A shaded, glossy 3D envelope. Unique gradient ids per copy avoid clashes."""
    body_l, body_d, flap_l, flap_d, edge = scheme
    b, f, g = f"mb{i}", f"mf{i}", f"mg{i}"
    return (
        '<svg viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg">'
        '<defs>'
        f'<linearGradient id="{b}" x1="32" y1="18" x2="32" y2="50" gradientUnits="userSpaceOnUse">'
        f'<stop offset="0" stop-color="{body_l}"/><stop offset="1" stop-color="{body_d}"/></linearGradient>'
        f'<linearGradient id="{f}" x1="32" y1="17" x2="32" y2="37" gradientUnits="userSpaceOnUse">'
        f'<stop offset="0" stop-color="{flap_l}"/><stop offset="1" stop-color="{flap_d}"/></linearGradient>'
        f'<radialGradient id="{g}" cx="0.32" cy="0.18" r="0.85">'
        '<stop offset="0" stop-color="#ffffff" stop-opacity="0.55"/>'
        '<stop offset="0.55" stop-color="#ffffff" stop-opacity="0"/></radialGradient>'
        '</defs>'
        # soft contact shadow grounding the envelope
        '<ellipse cx="32" cy="51.5" rx="19" ry="3" fill="#000000" opacity="0.18"/>'
        # envelope body with vertical light->saturated gradient (gives sheen)
        f'<rect x="9" y="18" width="46" height="32" rx="6" fill="url(#{b})"/>'
        # front bottom seam where the lower flaps meet — subtle depth cue
        f'<path d="M11.5 47 L32 33 L52.5 47" stroke="{flap_d}" stroke-width="1.2" '
        'stroke-opacity="0.35" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
        # folded-down top flap (darker, its own gradient)
        f'<path d="M11 18.5 H53 V21.5 L33.6 35.6 a3 3 0 0 1 -3.2 0 L11 21.5 Z" fill="url(#{f})"/>'
        # bright fold highlight along the flap edges — the key 3D read
        f'<path d="M10.5 19.5 L32 34.8 L53.5 19.5" stroke="{edge}" stroke-width="1.5" '
        'stroke-opacity="0.75" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
        # glossy top-left light wrap
        f'<rect x="9" y="18" width="46" height="32" rx="6" fill="url(#{g})"/>'
        '</svg>'
    )


_MAIL_COUNT = 13  # how many envelopes drift around


def _mail_svgs() -> list[str]:
    """One unique-id SVG per envelope, cycling through the color schemes."""
    return [_mail_svg(i, _MAIL_SCHEMES[i % len(_MAIL_SCHEMES)]) for i in range(_MAIL_COUNT)]


# Physics field: each envelope gets a random heading + speed, bounces off the
# viewport edges, and on collision the pair exchange momentum (elastic) so they
# visibly veer off in new directions. Runs from a 0-height components.html
# iframe (the only API that can execute DOM-manipulating JS), reaching into the
# parent page to append a fixed layer inside .stApp, behind the app content.
_MAIL_JS = """
<script>
(function(){
  var SVGS = __SVGS__;
  var win = window.parent, doc = win.document;

  // Streamlit reruns this — tear down any previous field + loop first.
  var old = doc.getElementById('mail-field');
  if (old) old.remove();
  if (win.__mailRAF) { win.cancelAnimationFrame(win.__mailRAF); win.__mailRAF = null; }

  var field = doc.createElement('div');
  field.id = 'mail-field';
  field.className = 'mail-field';
  field.setAttribute('aria-hidden', 'true');
  // Inside .stApp: above its opaque background, below the content (z-index:1).
  var host = doc.querySelector('.stApp') || doc.body;
  host.appendChild(field);

  var rnd = function(a, b){ return a + Math.random() * (b - a); };
  var vw = function(){ return doc.documentElement.clientWidth; };
  var vh = function(){ return doc.documentElement.clientHeight; };
  var W = vw(), H = vh();
  var reduce = win.matchMedia && win.matchMedia('(prefers-reduced-motion: reduce)').matches;

  var items = SVGS.map(function(svg){
    var size = rnd(78, 132);
    var el = doc.createElement('div');
    el.className = 'mail3d';
    el.style.width = size + 'px';
    el.style.height = size + 'px';
    el.style.opacity = rnd(0.16, 0.30).toFixed(3);
    el.innerHTML = svg;
    field.appendChild(el);
    var ang = rnd(0, Math.PI * 2), spd = reduce ? 0 : rnd(18, 46);
    return {
      el: el, size: size, r: size / 2,
      x: rnd(0, Math.max(1, W - size)), y: rnd(0, Math.max(1, H - size)),
      vx: Math.cos(ang) * spd, vy: Math.sin(ang) * spd,
      rot: rnd(-18, 18), vr: reduce ? 0 : rnd(-14, 14)
    };
  });

  function draw(p){
    p.el.style.transform = 'translate(' + p.x + 'px,' + p.y + 'px) rotate(' + p.rot + 'deg)';
  }
  items.forEach(draw);
  if (reduce) return;  // honor reduced-motion: place once, don't animate

  // Click anywhere -> envelopes are shoved away from the cursor (closer = harder).
  function shove(cx, cy){
    for (var i = 0; i < items.length; i++){
      var p = items[i];
      var dx = (p.x + p.r) - cx, dy = (p.y + p.r) - cy;
      var d = Math.hypot(dx, dy) || 1;
      var force = 720 / (1 + d / 90);
      p.vx += (dx / d) * force; p.vy += (dy / d) * force;
      p.vr += rnd(-30, 30);
    }
  }
  if (win.__mailClick) doc.removeEventListener('pointerdown', win.__mailClick, true);
  win.__mailClick = function(e){ shove(e.clientX, e.clientY); };
  doc.addEventListener('pointerdown', win.__mailClick, true);

  var last = null;
  function step(ts){
    if (last == null) last = ts;
    var dt = Math.min(0.05, (ts - last) / 1000); last = ts;
    W = vw(); H = vh();

    for (var i = 0; i < items.length; i++){
      var p = items[i];
      p.x += p.vx * dt; p.y += p.vy * dt; p.rot += p.vr * dt;
      // bounce off the screen edges
      if (p.x < 0){ p.x = 0; p.vx = Math.abs(p.vx); }
      else if (p.x + p.size > W){ p.x = W - p.size; p.vx = -Math.abs(p.vx); }
      if (p.y < 0){ p.y = 0; p.vy = Math.abs(p.vy); }
      else if (p.y + p.size > H){ p.y = H - p.size; p.vy = -Math.abs(p.vy); }
      // keep speed in a calm band: click-bursts decay, drift never stalls
      var sp = Math.hypot(p.vx, p.vy);
      if (sp > 70){ var f = Math.max(70 / sp, 0.97); p.vx *= f; p.vy *= f; }
      else if (sp > 0 && sp < 14){ var g = 14 / sp; p.vx *= g; p.vy *= g; }
      if (p.vr > 40) p.vr = 40; else if (p.vr < -40) p.vr = -40;
    }

    // pairwise elastic collisions — they push apart and swap normal velocity
    for (var a = 0; a < items.length; a++){
      for (var b = a + 1; b < items.length; b++){
        var p1 = items[a], p2 = items[b];
        var dx = (p2.x + p2.r) - (p1.x + p1.r);
        var dy = (p2.y + p2.r) - (p1.y + p1.r);
        var dist = Math.hypot(dx, dy), min = p1.r + p2.r;
        if (dist > 0 && dist < min){
          var nx = dx / dist, ny = dy / dist, overlap = (min - dist) / 2;
          p1.x -= nx * overlap; p1.y -= ny * overlap;
          p2.x += nx * overlap; p2.y += ny * overlap;
          var vn = (p2.vx - p1.vx) * nx + (p2.vy - p1.vy) * ny;
          if (vn < 0){
            p1.vx += vn * nx; p1.vy += vn * ny;
            p2.vx -= vn * nx; p2.vy -= vn * ny;
            p1.vr += rnd(-8, 8); p2.vr += rnd(-8, 8);  // spin kick on impact
          }
        }
      }
    }

    for (var k = 0; k < items.length; k++) draw(items[k]);
    win.__mailRAF = win.requestAnimationFrame(step);
  }
  win.__mailRAF = win.requestAnimationFrame(step);
})();
</script>
"""


def inject() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)
    # Decorative only — never let the background effect break the app.
    # components.html is the one API that actually executes JS that can reach
    # the parent page; st.html / st.iframe cannot, so we use it despite its
    # deprecation notice (a harmless console log; still supported).
    try:
        components.html(
            _MAIL_JS.replace("__SVGS__", json.dumps(_mail_svgs())),
            height=0,
        )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# HTML component helpers
# ---------------------------------------------------------------------------

def _e(v) -> str:
    return html.escape(str(v))


def hero(title: str, subtitle: str, pills: list[tuple[str, bool]]) -> None:
    pill_html = "".join(
        f'<span class="pill"><span class="dot {"dot-on" if on else "dot-off"}"></span>{_e(label)}</span>'
        for label, on in pills
    )
    st.markdown(
        f"""
        <div class="hero">
          <h1>{_e(title)}</h1>
          <p>{_e(subtitle)}</p>
          <div class="pills">{pill_html}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def section(title: str) -> None:
    st.markdown(
        f'<div class="section"><div class="bar"></div><h3>{_e(title)}</h3></div>',
        unsafe_allow_html=True,
    )


def metric_cards(items: list[tuple[str, object, str]]) -> None:
    """items: list of (label, value, accent) where accent in {'', 'warm', 'green'}."""
    cards = "".join(
        f'<div class="m-card {accent}"><div class="m-label">{_e(label)}</div>'
        f'<div class="m-value">{_e(value)}</div></div>'
        for label, value, accent in items
    )
    st.markdown(f'<div class="metric-grid">{cards}</div>', unsafe_allow_html=True)


def opportunity_header(opp: dict) -> str:
    """Markdown string for an expander label: optional PRIORITY tag + company/role."""
    company = opp.get("company", "Unknown")
    role = opp.get("role", "Unknown")
    prefix = ""
    if opp.get("priority"):
        tag = "2028 PRIORITY" if opp.get("batch_2028") else "PRIORITY"
        prefix = f":orange-background[ ⭐ {tag} ]  "
    return f"{prefix}**{company}**  ·  {role}"
