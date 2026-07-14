#!/usr/bin/env python3
"""Radar IA — agregador de titulares.

Lee los feeds de feeds.txt, los etiqueta con las categorias de categorias.txt,
acumula todo en docs/items.json y genera la web:

  index.html    la BANDEJA: lo pendiente de leer (app JS sobre items.json)
  YYYY-MM-DD.html  la pagina de cada dia (historico inmutable)
  archivo.html  indice de dias
  iconos/       el favicon real de cada fuente, descargado aqui (no en el navegador)

Sin dependencias: solo biblioteca estandar. Se ejecuta solo cada manana en GitHub Actions.
"""

import html
import json
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

RAIZ = Path(__file__).parent
WEB = RAIZ / "docs"          # GitHub Pages publica esta carpeta tal cual
ICONOS = WEB / "iconos"
ITEMS = WEB / "items.json"
META = WEB / "meta.json"     # fuentes y categorias, para que la web las pinte

UA = "Mozilla/5.0 (compatible; RadarIA/1.0; +https://github.com/proyectotestbuk/radar-ia)"
TIMEOUT = 25
MADRID = timezone(timedelta(hours=2))

# Dos diques contra el ruido, aprendidos cuando el feed de Hugging Face
# colo 823 titulares de todo su archivo en la primera prueba:
VENTANA_DIAS = 7   # nada mas viejo entra, aunque el feed lo sirva
TOPE_FUENTE = 25   # ninguna fuente puede inundar un dia

NS = {"atom": "http://www.w3.org/2005/Atom", "dc": "http://purl.org/dc/elements/1.1/"}


# ---------------------------------------------------------------- config

def slug(texto_):
    return re.sub(r"[^a-z0-9]+", "-", texto_.lower()).strip("-")


def leer_feeds():
    fuentes = []
    for linea in (RAIZ / "feeds.txt").read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        partes = [p.strip() for p in linea.split("|")]
        if len(partes) < 3:
            print(f"  ! linea mal formada, ignorada: {linea}")
            continue
        filtro = partes[3] if len(partes) > 3 else ""
        fuentes.append({
            "id": slug(partes[0]),
            "nombre": partes[0],
            "idioma": partes[1],
            "url": partes[2],
            "filtro": [p.strip().lower() for p in filtro.split(",") if p.strip()],
        })
    return fuentes


def leer_categorias():
    cats = []
    for linea in (RAIZ / "categorias.txt").read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        partes = [p.strip() for p in linea.split("|")]
        if len(partes) < 4:
            continue
        cats.append({
            "id": partes[0],
            "emoji": partes[1],
            "nombre": partes[2],
            "claves": [p.strip().lower() for p in partes[3].split(",") if p.strip()],
        })
    cats.append({"id": "otros", "emoji": "•", "nombre": "Otros", "claves": []})
    return cats


def categorizar(titulo, cats):
    t = titulo.lower()
    for c in cats:
        if any(k in t for k in c["claves"]):
            return c["id"]
    return "otros"


# ---------------------------------------------------------------- red

def descargar(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def bajar_favicon(fuente, ejemplo_url):
    """Guarda el favicon de la fuente en docs/iconos/. Si no hay, se usara la inicial."""
    destino = ICONOS / f"{fuente['id']}.png"
    if destino.exists():
        return destino.name

    dominio = urlparse(ejemplo_url).netloc
    if not dominio:
        return ""

    for candidata in (
        f"https://{dominio}/favicon.ico",
        f"https://www.google.com/s2/favicons?sz=64&domain={dominio}",
    ):
        try:
            datos = descargar(candidata)
            if len(datos) > 100:                      # descarta respuestas vacias o de error
                destino.write_bytes(datos)
                return destino.name
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            continue
    return ""


# ---------------------------------------------------------------- feeds

def texto(nodo):
    if nodo is None:
        return ""
    t = "".join(nodo.itertext()) if len(nodo) else (nodo.text or "")
    return html.unescape(re.sub(r"\s+", " ", t)).strip()


def fecha_iso(bruto):
    if not bruto:
        return ""
    try:
        return parsedate_to_datetime(bruto.strip()).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(bruto.strip().replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except ValueError:
        return ""


def parsear(xml_bytes):
    raiz = ET.fromstring(xml_bytes)
    items = []

    for it in raiz.iter("item"):                                   # RSS 2.0
        titulo, enlace = texto(it.find("title")), texto(it.find("link"))
        fecha = fecha_iso(texto(it.find("pubDate")) or texto(it.find("dc:date", NS)))
        if titulo and enlace:
            items.append((titulo, enlace, fecha))

    for it in raiz.iter(f"{{{NS['atom']}}}entry"):                 # Atom
        titulo = texto(it.find("atom:title", NS))
        nodo = it.find("atom:link[@rel='alternate']", NS) or it.find("atom:link", NS)
        enlace = nodo.get("href", "") if nodo is not None else ""
        fecha = fecha_iso(texto(it.find("atom:published", NS)) or texto(it.find("atom:updated", NS)))
        if titulo and enlace:
            items.append((titulo, enlace, fecha))

    return items


def es_reciente(fecha, limite):
    if not fecha:
        return True          # sin fecha se acepta: la dedup por URL evita repetirlo
    try:
        return datetime.fromisoformat(fecha) >= limite
    except ValueError:
        return True


def cosechar(fuentes, cats):
    nuevos, caidos = [], []
    limite = datetime.now(timezone.utc) - timedelta(days=VENTANA_DIAS)

    for f in fuentes:
        try:
            crudos = parsear(descargar(f["url"]))
        except (urllib.error.URLError, urllib.error.HTTPError, ET.ParseError, TimeoutError, OSError) as e:
            print(f"  FALLO    {f['nombre']}: {type(e).__name__} {e}")
            caidos.append(f["nombre"])
            continue

        items = [
            {
                "titulo": t,
                "url": u,
                "fecha": fe,
                "fuente": f["nombre"],
                "fuente_id": f["id"],
                "idioma": f["idioma"],
                "cat": categorizar(t, cats),
            }
            for t, u, fe in crudos
            if es_reciente(fe, limite)
            and (not f["filtro"] or any(p in t.lower() for p in f["filtro"]))
        ][:TOPE_FUENTE]

        descartados = len(crudos) - len(items)
        f["icono"] = bajar_favicon(f, items[0]["url"] if items else f["url"])
        nota = f"  (-{descartados} viejos/filtrados)" if descartados else ""
        print(f"  ok  {len(items):3d}  {f['nombre']}{nota}")
        nuevos.extend(items)

    return nuevos, caidos


def fusionar(cosecha, hoy, cats):
    """La URL es la identidad. Reetiqueta el historico por si cambiaron las categorias."""
    historico = json.loads(ITEMS.read_text(encoding="utf-8")) if ITEMS.exists() else []
    for viejo in historico:
        viejo["cat"] = categorizar(viejo["titulo"], cats)
        viejo.setdefault("fuente_id", slug(viejo["fuente"]))   # items anteriores a las categorias
        viejo.setdefault("idioma", "es")

    vistas = {i["url"] for i in historico}
    frescos = []
    for item in cosecha:
        if item["url"] in vistas:
            continue
        vistas.add(item["url"])
        item["dia"] = hoy                     # el dia en que ESTE radar lo vio por primera vez
        frescos.append(item)

    historico = frescos + historico
    ITEMS.write_text(json.dumps(historico, ensure_ascii=False, indent=1), encoding="utf-8")
    return frescos, historico


# ---------------------------------------------------------------- html

CSS = """
:root{--bg:#fbfaf8;--fg:#1a1a19;--dim:#6b6a66;--linea:#e5e2dc;--card:#fff;--ac:#b4530a;--ok:#2f7d55}
@media(prefers-color-scheme:dark){:root{--bg:#16161a;--fg:#e8e6e1;--dim:#918f89;--linea:#2c2c33;--card:#1d1d22;--ac:#e8945a;--ok:#6ac292}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 -apple-system,"Segoe UI",system-ui,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:900px;margin:0 auto;padding:24px 18px 80px}
header{border-bottom:2px solid var(--fg);padding-bottom:12px;margin-bottom:18px}
h1{font-size:1.45rem;margin:0;letter-spacing:-.02em}
h1 span{color:var(--ac)}
.sub{color:var(--dim);font-size:.84rem;margin-top:3px}
nav{margin-top:12px;display:flex;gap:6px;flex-wrap:wrap}
nav button,nav a{background:none;border:1px solid var(--linea);color:var(--fg);font:inherit;font-size:.82rem;
 padding:5px 12px;border-radius:20px;cursor:pointer;text-decoration:none}
nav button.on{background:var(--fg);color:var(--bg);border-color:var(--fg)}
nav button:hover,nav a:hover{border-color:var(--ac)}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:14px 0}
.chip{display:flex;align-items:center;gap:6px;background:var(--card);border:1px solid var(--linea);
 border-radius:20px;padding:4px 11px;font-size:.78rem;cursor:pointer;user-select:none}
.chip:hover{border-color:var(--ac)}
.chip.off{opacity:.35}
.chip img{width:15px;height:15px;border-radius:3px}
.chip .n{color:var(--dim);font-variant-numeric:tabular-nums}
ul{list-style:none;margin:0;padding:0}
li{display:flex;align-items:center;gap:12px;background:var(--card);border:1px solid var(--linea);
 border-radius:10px;padding:11px 13px;margin-bottom:7px}
li:hover{border-color:var(--ac)}
.ico{width:26px;height:26px;flex:0 0 26px;border-radius:5px;object-fit:contain;background:var(--bg)}
.ini{width:26px;height:26px;flex:0 0 26px;border-radius:5px;display:grid;place-items:center;
 background:var(--linea);font-size:.72rem;font-weight:700;color:var(--dim)}
.txt{flex:1;min-width:0}
.txt a{color:var(--fg);text-decoration:none;font-weight:500;display:block}
.txt a:hover{color:var(--ac)}
.meta{font-size:.72rem;color:var(--dim);margin-top:3px;display:flex;gap:7px;flex-wrap:wrap}
.cat{flex:0 0 auto;font-size:1.15rem;cursor:default}
.x{flex:0 0 auto;background:none;border:none;color:var(--dim);font-size:1.1rem;cursor:pointer;
 padding:2px 5px;border-radius:5px;line-height:1}
.x:hover{background:var(--linea);color:var(--ac)}
.vacio{color:var(--dim);font-style:italic;padding:34px 0;text-align:center}
input[type=search]{width:100%;padding:11px 13px;font-size:1rem;border:1px solid var(--linea);
 border-radius:9px;background:var(--card);color:var(--fg);margin-bottom:10px}
input[type=search]:focus{outline:2px solid var(--ac);border-color:transparent}
footer{margin-top:44px;padding-top:14px;border-top:1px solid var(--linea);color:var(--dim);font-size:.76rem}
footer button{background:none;border:1px solid var(--linea);color:var(--dim);font:inherit;font-size:.74rem;
 padding:3px 9px;border-radius:6px;cursor:pointer;margin-right:5px}
footer button:hover{border-color:var(--ac);color:var(--ac)}
h2{font-size:.76rem;text-transform:uppercase;letter-spacing:.09em;color:var(--dim);margin:26px 0 9px;font-weight:600}
"""

CAB = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{titulo}</title><style>{css}</style></head><body><div class="wrap">
<header><h1>Radar <span>IA</span></h1><div class="sub">{sub}</div>{nav}</header>
"""


def icono_html(fuente_id, nombre, iconos, clase="ico"):
    if iconos.get(fuente_id):
        return f'<img class="{clase}" src="iconos/{iconos[fuente_id]}" alt="" loading="lazy">'
    return f'<div class="ini">{html.escape(nombre[:2].upper())}</div>'


def li_estatico(item, iconos, cats_map):
    c = cats_map.get(item["cat"], {"emoji": "•", "nombre": ""})
    bandera = "🇬🇧" if item["idioma"] == "en" else "🇪🇸"
    return (
        f'<li>{icono_html(item["fuente_id"], item["fuente"], iconos)}'
        f'<div class="txt"><a href="{html.escape(item["url"])}" target="_blank" rel="noopener">'
        f'{html.escape(item["titulo"])}</a>'
        f'<div class="meta"><span>{html.escape(item["fuente"])}</span><span>{bandera}</span></div></div>'
        f'<span class="cat" title="{c["nombre"]}">{c["emoji"]}</span></li>'
    )


def pagina_dia(items, dia, iconos, cats_map, caidos, sello):
    nav = '<nav><a href="index.html">← Bandeja</a><a href="archivo.html">Archivo</a></nav>'
    cuerpo = ["<h2>Titulares vistos por primera vez este día</h2>", "<ul>"]
    cuerpo += [li_estatico(i, iconos, cats_map) for i in items] or ['<p class="vacio">Nada nuevo este día.</p>']
    cuerpo.append("</ul>")
    aviso = f"⚠️ Feeds caídos: {', '.join(caidos)}." if caidos else ""
    return (
        CAB.format(titulo=f"Radar IA — {dia}", css=CSS, sub=f"{len(items)} titulares · {dia}", nav=nav)
        + "\n".join(cuerpo)
        + f'<footer>Página de archivo (inmutable). Generada el {sello}. {aviso}</footer></div></body></html>'
    )


def pagina_archivo(historico, sello):
    nav = '<nav><a href="index.html">← Bandeja</a></nav>'
    dias = {}
    for i in historico:
        dias[i["dia"]] = dias.get(i["dia"], 0) + 1
    filas = "\n".join(
        f'<li><div class="txt"><a href="{d}.html">{d}</a></div>'
        f'<span class="meta">{n} titulares</span></li>'
        for d, n in sorted(dias.items(), reverse=True)
    )
    return (
        CAB.format(titulo="Radar IA — Archivo", css=CSS,
                   sub=f"{len(dias)} días · {len(historico)} titulares", nav=nav)
        + f"<h2>Histórico por días</h2><ul>{filas}</ul>"
        + f"<footer>Generado el {sello}.</footer></div></body></html>"
    )


APP = """
<div class="chips" id="chipsF"></div>
<div class="chips" id="chipsC"></div>
<input type="search" id="q" placeholder="Filtrar por texto: ollama, pgvector, licencia...">
<ul id="lista"></ul>
<footer>
  <div style="margin-bottom:8px">
    <button id="todo">Marcar todo como leído</button>
    <button id="exp">Exportar mi estado</button>
    <button id="imp">Importar</button>
    <input type="file" id="file" accept=".json" hidden>
  </div>
  Titulares tal cual llegan de la fuente — nadie los resume ni los ordena por interés.
  Lo leído y lo descartado se guarda <b>en este navegador</b> (no se sincroniza con otros equipos).
  <span id="sello"></span>
</footer>
<script>
const K='radarIA.v1';
const S=JSON.parse(localStorage.getItem(K)||'{"leidos":{},"borrados":{}}');
const guarda=()=>localStorage.setItem(K,JSON.stringify(S));
let D=[],M={},vista='pendientes',fOff=new Set(),cOff=new Set();

const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const bandera=l=>l==='en'?'\\uD83C\\uDDEC\\uD83C\\uDDE7':'\\uD83C\\uDDEA\\uD83C\\uDDF8';
const icono=i=>M.iconos[i.fuente_id]
  ? `<img class="ico" src="iconos/${M.iconos[i.fuente_id]}" alt="" loading="lazy">`
  : `<div class="ini">${esc(i.fuente.slice(0,2).toUpperCase())}</div>`;

function base(){
  if(vista==='leidos')   return D.filter(i=>S.leidos[i.url]);
  if(vista==='borrados') return D.filter(i=>S.borrados[i.url]);
  return D.filter(i=>!S.leidos[i.url] && !S.borrados[i.url]);   // la bandeja
}
function visibles(){
  const q=document.getElementById('q').value.toLowerCase().trim();
  return base().filter(i=>!fOff.has(i.fuente_id) && !cOff.has(i.cat)
    && (q.length<2 || i.titulo.toLowerCase().includes(q)));
}

function pinta(){
  const items=visibles();
  document.getElementById('lista').innerHTML = items.length ? items.map(i=>{
    const c=M.cats[i.cat]||{emoji:'\\u2022',nombre:''};
    return `<li data-u="${esc(i.url)}">${icono(i)}
      <div class="txt"><a href="${esc(i.url)}" target="_blank" rel="noopener" class="abrir">${esc(i.titulo)}</a>
      <div class="meta"><span>${esc(i.fuente)}</span><span>${bandera(i.idioma)}</span><span>${i.dia}</span></div></div>
      <span class="cat" title="${esc(c.nombre)}">${c.emoji}</span>
      <button class="x" title="${vista==='pendientes'?'Descartar':'Devolver a la bandeja'}">
        ${vista==='pendientes'?'\\u2715':'\\u21ba'}</button></li>`;
  }).join('') : `<p class="vacio">${vista==='pendientes'
      ? 'Bandeja vacía. Todo leído \\u2014 vuelve mañana.'
      : 'Nada por aquí.'}</p>`;

  // Abrir un titular = leerlo: desaparece de la bandeja y pasa a Leídos.
  document.querySelectorAll('.abrir').forEach(a=>a.onclick=e=>{
    const u=e.target.closest('li').dataset.u;
    if(vista==='pendientes'){ S.leidos[u]=Date.now(); guarda(); setTimeout(pinta,150); }
  });
  document.querySelectorAll('.x').forEach(b=>b.onclick=e=>{
    const u=e.target.closest('li').dataset.u;
    if(vista==='pendientes'){ S.borrados[u]=Date.now(); }
    else { delete S.borrados[u]; delete S.leidos[u]; }   // devolver a la bandeja
    guarda(); pinta();
  });
  chips();
}

function chips(){
  const b=base();
  const cuenta=(k,v)=>b.filter(i=>i[k]===v).length;
  document.getElementById('chipsF').innerHTML = M.fuentes.map(f=>{
    const n=cuenta('fuente_id',f.id);
    const ic=M.iconos[f.id]?`<img src="iconos/${M.iconos[f.id]}" alt="">`:'';
    return `<span class="chip ${fOff.has(f.id)?'off':''}" data-f="${f.id}">${ic}
      ${esc(f.nombre)} <span class="n">${n}</span></span>`;
  }).join('');
  document.getElementById('chipsC').innerHTML = Object.entries(M.cats).map(([id,c])=>{
    const n=cuenta('cat',id);
    return `<span class="chip ${cOff.has(id)?'off':''}" data-c="${id}">${c.emoji}
      ${esc(c.nombre)} <span class="n">${n}</span></span>`;
  }).join('');
  document.querySelectorAll('[data-f]').forEach(el=>el.onclick=()=>{
    const f=el.dataset.f; fOff.has(f)?fOff.delete(f):fOff.add(f); pinta();
  });
  document.querySelectorAll('[data-c]').forEach(el=>el.onclick=()=>{
    const c=el.dataset.c; cOff.has(c)?cOff.delete(c):cOff.add(c); pinta();
  });
}

document.querySelectorAll('nav button[data-v]').forEach(b=>b.onclick=()=>{
  vista=b.dataset.v;
  document.querySelectorAll('nav button[data-v]').forEach(x=>x.classList.toggle('on',x===b));
  pinta();
});
document.getElementById('q').addEventListener('input',pinta);
document.getElementById('todo').onclick=()=>{
  if(!confirm('¿Marcar como leídos todos los titulares de la bandeja?'))return;
  base().forEach(i=>S.leidos[i.url]=Date.now()); guarda(); pinta();
};
document.getElementById('exp').onclick=()=>{
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([JSON.stringify(S)],{type:'application/json'}));
  a.download='radar-ia-estado.json'; a.click();
};
document.getElementById('imp').onclick=()=>document.getElementById('file').click();
document.getElementById('file').onchange=e=>{
  const r=new FileReader();
  r.onload=()=>{ const x=JSON.parse(r.result);
    Object.assign(S.leidos,x.leidos||{}); Object.assign(S.borrados,x.borrados||{});
    guarda(); pinta(); };
  r.readAsText(e.target.files[0]);
};

Promise.all([fetch('items.json').then(r=>r.json()),fetch('meta.json').then(r=>r.json())])
  .then(([d,m])=>{ D=d; M=m; document.getElementById('sello').textContent='Última cosecha: '+M.sello+'.'; pinta(); });
</script>
"""


def pagina_app(historico, sello, caidos):
    nav = (
        '<nav>'
        '<button data-v="pendientes" class="on">Bandeja</button>'
        '<button data-v="leidos">Leídos</button>'
        '<button data-v="borrados">Descartados</button>'
        '<a href="archivo.html">Archivo por días</a>'
        '</nav>'
    )
    aviso = f" · ⚠️ Feeds caídos hoy: {', '.join(caidos)}" if caidos else ""
    sub = f"Lo que no abras, sigue aquí mañana{aviso}"
    return CAB.format(titulo="Radar IA", css=CSS, sub=sub, nav=nav) + APP + "</div></body></html>"


# ---------------------------------------------------------------- main

def main():
    ahora = datetime.now(MADRID)
    hoy = ahora.strftime("%Y-%m-%d")
    sello = ahora.strftime("%d/%m/%Y a las %H:%M")
    WEB.mkdir(exist_ok=True)
    ICONOS.mkdir(exist_ok=True)

    print(f"Radar IA — cosecha del {hoy}")
    fuentes, cats = leer_feeds(), leer_categorias()
    cosecha, caidos = cosechar(fuentes, cats)
    frescos, historico = fusionar(cosecha, hoy, cats)
    print(f"\n  {len(frescos)} titulares nuevos · {len(historico)} en el histórico")

    iconos = {f["id"]: f.get("icono", "") for f in fuentes}
    cats_map = {c["id"]: {"emoji": c["emoji"], "nombre": c["nombre"]} for c in cats}
    META.write_text(json.dumps({
        "fuentes": [{"id": f["id"], "nombre": f["nombre"], "idioma": f["idioma"]} for f in fuentes],
        "iconos": iconos,
        "cats": cats_map,
        "sello": sello,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    (WEB / "index.html").write_text(pagina_app(historico, sello, caidos), encoding="utf-8")
    (WEB / "archivo.html").write_text(pagina_archivo(historico, sello), encoding="utf-8")
    del_dia = [i for i in historico if i["dia"] == hoy]
    (WEB / f"{hoy}.html").write_text(pagina_dia(del_dia, hoy, iconos, cats_map, caidos, sello), encoding="utf-8")

    if len(caidos) == len(fuentes):
        print("\n  TODOS los feeds han fallado — algo va mal (¿red?).")
        return 1
    print(f"  docs/ generada. Feeds caídos: {caidos or 'ninguno'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
