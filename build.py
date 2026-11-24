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
META = WEB / "meta.json"      # fuentes, categorias y config, para que la web las pinte
ESTADO = WEB / "estado.tsv"   # la tabla de estado: la escribe la WEB, no el build
CLASIF = RAIZ / "clasificacion.tsv"  # tipo e interes por URL: lo escribe clasifica.py (Claude Haiku), desde el 20/11/2026

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


def leer_config():
    cfg = {"repo": "", "corte": "1970-01-01", "retencion_dias": "90"}
    for linea in (RAIZ / "config.txt").read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        k, v = linea.split("=", 1)
        cfg[k.strip()] = v.strip()
    return cfg


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


def leer_clasificacion():
    """url -> (tipo, interes, motivo). Si un titular esta aqui, manda sobre las palabras clave."""
    d = {}
    if CLASIF.exists():
        for linea in CLASIF.read_text(encoding="utf-8").splitlines():
            if linea.startswith("#") or not linea.strip():
                continue
            p = linea.split("\t")
            if len(p) >= 3:
                d[p[0]] = (p[1], p[2], p[3] if len(p) > 3 else "")
    return d


def etiquetar(item, cats, clasif):
    c = clasif.get(item["url"])
    if c and any(x["id"] == c[0] for x in cats):
        item["cat"], item["interes"], item["motivo"] = c
        if item["interes"] == "nada" and item["fuente"].startswith(("Anthropic", "Claude")):
            item["interes"], item["cat"] = "medio", "claude"   # lo oficial de Anthropic nunca es morralla (Juan, 20/11/2026)
    else:
        item["cat"] = categorizar(item["titulo"], cats)
        item["interes"] = "?"          # aun sin pasar por el filtro
        item.pop("motivo", None)


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
    clasif = leer_clasificacion()
    for viejo in historico:
        etiquetar(viejo, cats, clasif)
        viejo.setdefault("fuente_id", slug(viejo["fuente"]))   # items anteriores a las categorias
        viejo.setdefault("idioma", "es")

    vistas = {i["url"] for i in historico}
    frescos = []
    for item in cosecha:
        if item["url"] in vistas:
            continue
        vistas.add(item["url"])
        item["dia"] = hoy                     # el dia en que ESTE radar lo vio por primera vez
        etiquetar(item, cats, clasif)
        frescos.append(item)

    historico = frescos + historico
    ITEMS.write_text(json.dumps(historico, ensure_ascii=False, indent=1), encoding="utf-8")
    return frescos, historico


# ---------------------------------------------------------------- html

CSS = """
/* Radar IA — rediseño del 24/11/2026: barra lateral con filtros, fondo claro, todo el ancho, 18 px. */
:root{--bg:#f5f7f9;--panel:#ffffff;--fg:#16212b;--dim:#5a6672;--linea:#dbe1e7;
 --ac:#0a6b86;--ac-suave:#e6f2f5;--estrella:#b57400;--lat:300px}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg);color:var(--fg);font:18px/1.5 "Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,sans-serif;
 -webkit-font-smoothing:antialiased}
a{color:inherit}
button{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--ac);outline-offset:2px}
h1{font-size:1.15rem;margin:0;font-weight:700;letter-spacing:-.01em;white-space:nowrap}
h1 .ping{display:inline-block;width:.62em;height:.62em;border-radius:50%;margin-right:.45em;vertical-align:.05em;
 background:radial-gradient(circle,var(--ac) 32%,transparent 34%),radial-gradient(circle,transparent 58%,var(--ac) 60%,var(--ac) 66%,transparent 68%)}
.sub{color:var(--dim);font-size:.85rem;margin:0}

/* ---------- la app: cabecera fina + lateral fijo + lista */
.top{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:18px;height:52px;
 padding:0 24px;background:var(--panel);border-bottom:1px solid var(--linea)}
.top .sub{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.top .sello{flex:0 0 auto;color:var(--dim);font-size:.8rem;white-space:nowrap}
.marco{display:grid;grid-template-columns:var(--lat) minmax(0,1fr);min-height:calc(100vh - 52px)}
#lateral{position:sticky;top:52px;align-self:start;height:calc(100vh - 52px);overflow-y:auto;
 background:var(--panel);border-right:1px solid var(--linea);padding:16px 14px 28px;font-size:.86rem}
#lateral h2{font-size:.86rem;font-weight:700;margin:22px 0 6px;color:var(--fg)}
.cab-filtro{display:flex;align-items:baseline;justify-content:space-between;gap:8px}
.cab-filtro h2{margin:0}
.cab-filtro button{background:none;border:0;color:var(--ac);font-size:.78rem;cursor:pointer;padding:2px 4px;text-decoration:underline;text-underline-offset:2px}
.seccion{margin-top:20px}
input[type=search]{width:100%;padding:9px 11px;font-size:.95rem;border:1px solid var(--linea);border-radius:6px;
 background:var(--bg);color:var(--fg)}
input[type=search]:focus{outline:2px solid var(--ac);border-color:transparent}
nav.vistas{display:flex;flex-direction:column;margin-top:14px}
nav.vistas button,nav.vistas a{display:flex;justify-content:space-between;align-items:center;background:none;border:0;
 border-left:3px solid transparent;text-align:left;padding:6px 10px;font-size:.92rem;cursor:pointer;text-decoration:none;color:var(--fg)}
nav.vistas button:hover,nav.vistas a:hover{background:var(--bg)}
nav.vistas button.on{border-left-color:var(--ac);background:var(--ac-suave);font-weight:600}
nav.vistas .n,.filtros .n{color:var(--dim);font-variant-numeric:tabular-nums;font-weight:400}
ul.filtros{list-style:none;margin:0;padding:0}
ul.filtros li{display:flex;align-items:center;gap:6px;padding:2px 0;margin:0;border:0;background:none}
ul.filtros label{flex:1;min-width:0;display:flex;align-items:center;gap:7px;cursor:pointer;padding:3px 4px;border-radius:4px}
ul.filtros label:hover{background:var(--bg)}
ul.filtros input{accent-color:var(--ac);margin:0;flex:0 0 auto}
ul.filtros img{width:16px;height:16px;border-radius:3px;flex:0 0 16px}
ul.filtros .nom{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
ul.filtros li.cero .nom{color:var(--dim)}
ul.filtros .solo{visibility:hidden;background:none;border:0;color:var(--ac);font-size:.74rem;cursor:pointer;padding:2px 4px}
ul.filtros li:hover .solo,ul.filtros .solo:focus{visibility:visible}
.acciones{margin-top:26px;padding-top:14px;border-top:1px solid var(--linea);display:flex;flex-direction:column;gap:6px}
.acciones button,.acciones a{background:none;border:1px solid var(--linea);border-radius:6px;padding:6px 10px;
 font-size:.82rem;cursor:pointer;text-align:left;text-decoration:none;color:var(--fg)}
.acciones button:hover,.acciones a:hover{border-color:var(--ac);color:var(--ac)}
main{min-width:0;padding:6px 28px 60px}
.resumen{color:var(--dim);font-size:.85rem;margin:12px 0 4px}

/* ---------- filas de titulares (app y páginas fijas) */
ul{list-style:none;margin:0;padding:0}
li{display:flex;align-items:center;gap:14px;padding:12px 10px;border-bottom:1px solid var(--linea);border-left:4px solid transparent}
li:hover{background:var(--panel)}
li.alto{background:var(--ac-suave);border-left-color:var(--ac)}
.ico{width:26px;height:26px;flex:0 0 26px;border-radius:5px;object-fit:contain;background:var(--panel)}
.ini{width:26px;height:26px;flex:0 0 26px;border-radius:5px;display:grid;place-items:center;
 background:var(--linea);font-size:.7rem;font-weight:700;color:var(--dim)}
.txt{flex:1;min-width:0}
.txt a{color:var(--fg);text-decoration:none;font-weight:500;display:block;max-width:78ch}
.txt a:hover{color:var(--ac);text-decoration:underline;text-underline-offset:3px}
.txt a:visited{color:#4b5560}
.meta{font-size:.78rem;color:var(--dim);margin-top:3px;display:flex;gap:4px 12px;flex-wrap:wrap}
.alto-et{color:var(--ac);font-weight:600}
.cat{flex:0 0 auto;font-size:1.1rem;cursor:default}
.fav{flex:0 0 auto;background:none;border:0;font-size:1.3rem;color:var(--estrella);cursor:pointer;padding:4px;line-height:1}
.x{flex:0 0 auto;background:none;border:0;color:var(--dim);font-size:1.05rem;cursor:pointer;padding:4px 7px;border-radius:5px;line-height:1}
.x:hover{background:var(--linea);color:var(--fg)}
.vacio{color:var(--dim);padding:40px 0;text-align:center}
footer{margin-top:40px;padding-top:14px;border-top:1px solid var(--linea);color:var(--dim);font-size:.8rem;max-width:90ch}
code{font-size:.9em}

/* ---------- páginas fijas (archivo y días) */
.wrap{max-width:1100px;margin:0 auto;padding:20px 24px 70px}
.wrap header{display:flex;align-items:center;gap:18px;flex-wrap:wrap;border-bottom:1px solid var(--linea);padding-bottom:12px;margin-bottom:8px}
.wrap header nav{display:flex;gap:14px}
.wrap header nav a{color:var(--ac);font-size:.9rem}
.wrap h2{font-size:.95rem;margin:20px 0 6px}

/* ---------- móvil: el lateral se pliega */
.solo-movil{display:none}
#velo{display:none}
@media (max-width:860px){
 body{font-size:17px}
 .top{padding:0 14px;gap:10px}
 .top .sello{display:none}
 .solo-movil{display:inline-flex;align-items:center;gap:6px;background:var(--panel);border:1px solid var(--linea);
  border-radius:6px;padding:5px 11px;font-size:.88rem;cursor:pointer}
 .marco{display:block}
 #lateral{position:fixed;top:0;left:0;bottom:0;height:auto;width:min(86vw,330px);z-index:40;
  transform:translateX(-102%);transition:transform .2s ease;box-shadow:0 0 30px rgba(10,30,45,.18)}
 body.abierto #lateral{transform:none}
 body.abierto #velo{display:block;position:fixed;inset:0;background:rgba(10,30,45,.35);z-index:30}
 main{padding:4px 12px 50px}
 li{gap:10px;padding:11px 6px}
 .cat{display:none}
 ul.filtros .solo{visibility:visible}
}
@media (prefers-reduced-motion:reduce){#lateral{transition:none}}
"""

CAB = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{titulo}</title><style>{css}</style></head><body><div class="wrap">
<header><h1><span class="ping" aria-hidden="true"></span>Radar IA</h1><p class="sub">{sub}</p>{nav}</header>
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
  <div class="seccion cab-filtro"><h2>Fuentes</h2>
    <span><button id="fTodas">todas</button><button id="fNinguna">ninguna</button></span></div>
  <ul class="filtros" id="chipsF"></ul>
  <div class="seccion cab-filtro"><h2>Temas</h2>
    <span><button id="cTodas">todos</button><button id="cNinguna">ninguno</button></span></div>
  <ul class="filtros" id="chipsC"></ul>
  <div class="acciones">
    <button id="todo">Marcar la bandeja como leída</button>
    <button id="llave">🔑 Conectar con GitHub</button>
    <button id="salir" hidden>Desconectar</button>
    <a href="archivo.html">Archivo por días</a>
  </div>
</aside>
<main>
<p class="resumen" id="resumen"></p>
<ul id="lista"></ul>
<footer>
  Titulares tal cual llegan de la fuente. Desde el 20/11/2026 Claude Haiku pone a cada uno su tipo, su interés y un motivo; la morralla sale de la bandeja y lo que interesa (★ interesa) va primero.
  Abrir un titular lo pasa a Leídos; la estrella lo guarda en Guardados. El estado (leído / guardado / descartado) vive en <code>docs/estado.tsv</code>, <b>dentro del repo</b>:
  se sincroniza entre todos tus equipos. Los filtros se recuerdan en este navegador.
</footer>
</main>
</div>
<div id="velo"></div>
<script>
// ---------------------------------------------------------------- estado
// El repo ES la base de datos. estado.tsv es la tabla:  url <TAB> leido|descartado <TAB> fecha
// Se lee por la API de GitHub (siempre la última version + su sha, que hace falta para escribir).
// Se escribe con el token del usuario, guardado solo en ESTE navegador y jamas en el repo.

const TK='radarIA.token';
let D=[],M={},E={},sha=null,vista='pendientes',fOff=new Set(),cOff=new Set(),pend=null;

const token=()=>localStorage.getItem(TK)||'';
const cabeceras=()=>token()?{Authorization:'Bearer '+token()}:{};
const api=p=>`https://api.github.com/repos/${M.repo}/contents/${p}`;
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const bandera=l=>l==='en'?'\\uD83C\\uDDEC\\uD83C\\uDDE7':'\\uD83C\\uDDEA\\uD83C\\uDDF8';
const icono=i=>M.iconos[i.fuente_id]
  ? `<img class="ico" src="iconos/${M.iconos[i.fuente_id]}" alt="" loading="lazy">`
  : `<div class="ini">${esc(i.fuente.slice(0,2).toUpperCase())}</div>`;

const aviso=t=>{document.getElementById('sello').textContent=t;};

function parseTSV(txt){
  const e={};
  txt.split('\\n').forEach(l=>{
    if(!l.trim()||l.startsWith('#'))return;
    const [url,est,fecha]=l.split('\\t');
    if(url&&est) e[url.trim()]={est:est.trim(),fecha:(fecha||'').trim()};
  });
  return e;
}
const aTSV=e=>['# Radar IA — estado.  url <TAB> leido|descartado <TAB> fecha',
  ...Object.entries(e).map(([u,v])=>`${u}\\t${v.est}\\t${v.fecha}`)].join('\\n')+'\\n';

async function cargarEstado(){
  try{
    const r=await fetch(api('docs/estado.tsv')+'?ref=main',
      {headers:{...cabeceras(),Accept:'application/vnd.github+json'},cache:'no-store'});
    if(!r.ok) throw new Error('HTTP '+r.status);
    const j=await r.json();
    sha=j.sha;
    E=parseTSV(decodeURIComponent(escape(atob(j.content.replace(/\\n/g,'')))));
  }catch(err){ console.warn('estado.tsv no leído:',err); E={}; sha=null; }
}

// Un commit por rafaga, no por clic: se espera 1,5 s de calma antes de escribir.
function programaGuardado(){
  if(!token()){ aviso('⚠️ Sin conectar: estos cambios NO se guardan.'); return; }
  clearTimeout(pend);
  aviso('Guardando…');
  pend=setTimeout(()=>guardar(),1500);
}

async function guardar(reintento){
  const cuerpo=btoa(unescape(encodeURIComponent(aTSV(E))));
  const r=await fetch(api('docs/estado.tsv'),{
    method:'PUT',
    headers:{...cabeceras(),Accept:'application/vnd.github+json','Content-Type':'application/json'},
    body:JSON.stringify({message:'estado: '+new Date().toISOString().slice(0,16).replace('T',' '),
                         content:cuerpo, sha:sha||undefined, branch:'main'})
  });
  if(r.ok){ sha=(await r.json()).content.sha; aviso('✔ Guardado en el repo.'); return; }
  if(r.status===409 && !reintento){        // otro equipo escribió antes: fusionar y reintentar
    const mio={...E};
    await cargarEstado();
    Object.entries(mio).forEach(([u,v])=>{                 // gana la marca mas reciente
      if(!E[u] || (v.fecha||'') >= (E[u].fecha||'')) E[u]=v;
    });
    return guardar(true);
  }
  aviso(r.status===401||r.status===403
    ? '❌ Token rechazado (¿caducado o sin permiso de escritura?).'
    : '❌ No se pudo guardar (HTTP '+r.status+').');
}

function marca(url,est){
  if(est) E[url]={est,fecha:new Date().toISOString().slice(0,10)};
  else delete E[url];
  programaGuardado();
}

// ---------------------------------------------------------------- vistas

function base(){
  const vivo=i=>i.dia>=M.corte;                      // marca de corte de config.txt
  if(vista==='leidos')   return D.filter(i=>vivo(i)&&E[i.url]?.est==='leido');
  if(vista==='borrados') return D.filter(i=>vivo(i)&&E[i.url]?.est==='descartado');
  if(vista==='guardados') return D.filter(i=>vivo(i)&&E[i.url]?.est==='guardado');
  if(vista==='morralla') return D.filter(i=>vivo(i)&&!E[i.url]&&i.interes==='nada');
  const orden={alto:0,medio:1,'?':2};
  return D.filter(i=>vivo(i)&&!E[i.url]&&i.interes!=='nada')   // la bandeja: sin marca y sin morralla
          .sort((a,b)=>(orden[a.interes]??2)-(orden[b.interes]??2));
}
function visibles(){
  const q=document.getElementById('q').value.toLowerCase().trim();
  return base().filter(i=>!fOff.has(i.fuente_id)&&!cOff.has(i.cat)
    && (q.length<2 || i.titulo.toLowerCase().includes(q)));
}

function cuentas(){   // número de titulares junto a cada pestaña
  const v0=vista;
  document.querySelectorAll('nav button[data-v]').forEach(b=>{
    vista=b.dataset.v; b.innerHTML=`${b.dataset.t} <span class="n">${base().length}</span>`; });
  vista=v0;
}

function pinta(){
  cuentas();
  const items=visibles();
  document.getElementById('lista').innerHTML = items.length ? items.map(i=>{
    const c=M.cats[i.cat]||{emoji:'\\u2022',nombre:''};
    return `<li data-u="${esc(i.url)}"${i.interes==='alto'?' class="alto"':''}>${icono(i)}
      <div class="txt"><a href="${esc(i.url)}" target="_blank" rel="noopener" class="abrir">${esc(i.titulo)}</a>
      <div class="meta">${i.interes==='alto'?'<span class="alto-et">\u2605 interesa</span>':''}<span>${esc(i.fuente)}</span><span>${bandera(i.idioma)}</span><span>${i.dia}</span>${i.motivo?`<span>${esc(i.motivo)}</span>`:''}</div></div>
      <span class="cat" title="${esc(c.nombre)}">${c.emoji}</span>
      <button class="fav" title="${E[i.url]?.est==='guardado'?'Quitar de guardados':'Guardar como favorito'}">${E[i.url]?.est==='guardado'?'\\u2605':'\\u2606'}</button>
      <button class="x" title="${vista==='pendientes'||vista==='morralla'?'Descartar':'Devolver a la bandeja'}">
        ${vista==='pendientes'||vista==='morralla'?'\\u2715':'\\u21ba'}</button></li>`;
  }).join('') : `<p class="vacio">${vista==='pendientes'
      ? 'Bandeja vacía. Todo leído \\u2014 vuelve mañana.'
      : 'Nada por aquí.'}</p>`;

  // Abrir un titular = leerlo: sale de la bandeja y pasa a Leídos.
  document.querySelectorAll('.abrir').forEach(a=>a.onclick=e=>{
    if(vista!=='pendientes'&&vista!=='morralla') return;
    marca(e.target.closest('li').dataset.u,'leido');
    setTimeout(pinta,150);
  });
  document.querySelectorAll('.fav').forEach(b=>b.onclick=e=>{
    const u=e.target.closest('li').dataset.u;
    marca(u, E[u]?.est==='guardado' ? 'leido' : 'guardado');
    pinta();
  });
  document.querySelectorAll('.x').forEach(b=>b.onclick=e=>{
    const u=e.target.closest('li').dataset.u;
    marca(u, vista==='pendientes'||vista==='morralla' ? 'descartado' : null);   // en las otras vistas, devuelve
    pinta();
  });
  chips();
}

// Filtros del lateral: casilla = quitar o poner; «solo» = dejar solo esa. Se recuerdan en este navegador.
const FK='radarIA.filtros';
function guardaFiltros(){ try{ localStorage.setItem(FK,JSON.stringify({f:[...fOff],c:[...cOff]})); }catch(e){} }
function leeFiltros(){ try{ const o=JSON.parse(localStorage.getItem(FK)||'{}');
  fOff=new Set(o.f||[]); cOff=new Set(o.c||[]); }catch(e){} }

// Fuentes = las de feeds.txt más las retiradas que aún tienen titulares en el histórico.
let FU=null;
function fuentes(){
  if(FU) return FU;
  const ya=new Set(M.fuentes.map(f=>f.id)), extra={};
  D.forEach(i=>{ if(!ya.has(i.fuente_id)) extra[i.fuente_id]=i.fuente; });
  return FU=[...M.fuentes, ...Object.entries(extra).map(([id,nombre])=>({id,nombre:nombre+' (retirada)'}))];
}
function chips(){
  const b=base(), cuenta=(k,v)=>b.filter(i=>i[k]===v).length;
  const fila=(tipo,id,ic,nom,n,off)=>`<li class="${n?'':'cero'}"><label>
      <input type="checkbox" data-${tipo}="${id}" ${off?'':'checked'}>${ic}<span class="nom">${nom}</span>
      <span class="n">${n}</span></label><button class="solo" data-solo-${tipo}="${id}" title="Ver solo esta">solo</button></li>`;
  document.getElementById('chipsF').innerHTML=fuentes()
    .map(f=>({f,n:cuenta('fuente_id',f.id)})).sort((a,z)=>z.n-a.n||a.f.nombre.localeCompare(z.f.nombre))
    .map(({f,n})=>fila('f',f.id,M.iconos[f.id]?`<img src="iconos/${M.iconos[f.id]}" alt="">`:'<span class="ini" style="width:16px;height:16px;flex-basis:16px;font-size:.55rem">'+esc(f.nombre.slice(0,1))+'</span>',
      esc(f.nombre),n,fOff.has(f.id))).join('');
  document.getElementById('chipsC').innerHTML=Object.entries(M.cats)
    .map(([id,c])=>fila('c',id,`<span aria-hidden="true">${c.emoji}</span>`,esc(c.nombre),cuenta('cat',id),cOff.has(id))).join('');
  document.querySelectorAll('input[data-f]').forEach(el=>el.onchange=()=>{
    el.checked?fOff.delete(el.dataset.f):fOff.add(el.dataset.f); guardaFiltros(); pinta(); });
  document.querySelectorAll('input[data-c]').forEach(el=>el.onchange=()=>{
    el.checked?cOff.delete(el.dataset.c):cOff.add(el.dataset.c); guardaFiltros(); pinta(); });
  document.querySelectorAll('[data-solo-f]').forEach(el=>el.onclick=()=>{
    fOff=new Set(fuentes().map(f=>f.id).filter(x=>x!==el.dataset.soloF)); guardaFiltros(); pinta(); });
  document.querySelectorAll('[data-solo-c]').forEach(el=>el.onclick=()=>{
    cOff=new Set(Object.keys(M.cats).filter(x=>x!==el.dataset.soloC)); guardaFiltros(); pinta(); });
  const filtrados=fOff.size+cOff.size, v=visibles().length;
  document.getElementById('resumen').textContent=
    `${v} titulares a la vista`+(filtrados?`; ${fOff.size} fuentes y ${cOff.size} temas quitados`:'');
  document.getElementById('abreFiltros').textContent=filtrados?`Filtros (${filtrados})`:'Filtros';
}
document.getElementById('fTodas').onclick=()=>{fOff.clear();guardaFiltros();pinta();};
document.getElementById('fNinguna').onclick=()=>{fOff=new Set(fuentes().map(f=>f.id));guardaFiltros();pinta();};
document.getElementById('cTodas').onclick=()=>{cOff.clear();guardaFiltros();pinta();};
document.getElementById('cNinguna').onclick=()=>{cOff=new Set(Object.keys(M.cats));guardaFiltros();pinta();};
// Móvil: el lateral es un cajón que se abre con «Filtros» y se cierra tocando fuera o con Escape.
document.getElementById('abreFiltros').onclick=()=>document.body.classList.toggle('abierto');
document.getElementById('velo').onclick=()=>document.body.classList.remove('abierto');
document.addEventListener('keydown',e=>{ if(e.key==='Escape') document.body.classList.remove('abierto'); });

// ---------------------------------------------------------------- controles

document.querySelectorAll('nav button[data-v]').forEach(b=>b.onclick=()=>{
  vista=b.dataset.v;
  document.querySelectorAll('nav button[data-v]').forEach(x=>x.classList.toggle('on',x===b));
  pinta();
});
document.getElementById('q').addEventListener('input',pinta);
document.getElementById('todo').onclick=()=>{
  if(!confirm('¿Marcar como leídos todos los titulares de la bandeja?'))return;
  base().forEach(i=>E[i.url]={est:'leido',fecha:new Date().toISOString().slice(0,10)});
  programaGuardado(); pinta();
};
document.getElementById('llave').onclick=()=>{
  const t=prompt('Pega tu token de GitHub (fine-grained, permiso Contents: write SOLO en radar-ia).\\n'
    +'Se guarda únicamente en este navegador.');
  if(t){ localStorage.setItem(TK,t.trim()); arranca(); }
};
document.getElementById('salir').onclick=()=>{
  localStorage.removeItem(TK);
  location.reload();
};

async function arranca(){
  const conectado=!!token();
  document.getElementById('llave').hidden=conectado;
  document.getElementById('salir').hidden=!conectado;
  await cargarEstado();
  pinta();
  aviso(conectado
    ? 'Conectado · última cosecha: '+M.sello
    : '⚠️ Solo lectura: conecta con GitHub para que lo que leas se guarde.');
}

Promise.all([fetch('items.json',{cache:'no-store'}).then(r=>r.json()),
             fetch('meta.json',{cache:'no-store'}).then(r=>r.json())])
  .then(([d,m])=>{ D=d; M=m; leeFiltros(); arranca(); });
</script>
"""


def pagina_app(historico, sello, caidos):
    nav = (
        '<nav class="vistas" aria-label="Vistas">'
        '<button data-v="pendientes" data-t="Bandeja" class="on">Bandeja</button>'
        '<button data-v="guardados" data-t="★ Guardados" title="Tus favoritos: pulsa la estrella de un titular">★ Guardados</button>'
        '<button data-v="leidos" data-t="Leídos">Leídos</button>'
        '<button data-v="borrados" data-t="Descartados">Descartados</button>'
        '<button data-v="morralla" data-t="Morralla" title="Lo que Claude Haiku considera que no aporta">Morralla</button>'
        '</nav>'
    )
    aviso = f". ⚠️ Feeds caídos hoy: {html.escape(', '.join(caidos))}" if caidos else ""
    sub = f"Lo que no abras sigue aquí mañana{aviso}"
    cab = (
        '<!doctype html><html lang="es"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>Radar IA</title><style>{CSS}</style></head><body>'
        '<header class="top"><button id="abreFiltros" class="solo-movil" aria-controls="lateral">Filtros</button>'
        '<h1><span class="ping" aria-hidden="true"></span>Radar IA</h1>'
        f'<p class="sub">{sub}</p><span class="sello" id="sello"></span></header>'
        '<div class="marco"><aside id="lateral" aria-label="Filtros">'
        '<input type="search" id="q" placeholder="Buscar en los titulares: ollama, pgvector…" aria-label="Buscar">'
        + nav
    )
    return cab + APP + "</body></html>"


# ---------------------------------------------------------------- main

def main():
    ahora = datetime.now(MADRID)
    hoy = ahora.strftime("%Y-%m-%d")
    sello = ahora.strftime("%d/%m/%Y a las %H:%M")
    WEB.mkdir(exist_ok=True)
    ICONOS.mkdir(exist_ok=True)

    print(f"Radar IA — cosecha del {hoy}")
    cfg = leer_config()
    fuentes, cats = leer_feeds(), leer_categorias()
    cosecha, caidos = cosechar(fuentes, cats)
    frescos, historico = fusionar(cosecha, hoy, cats)
    print(f"\n  {len(frescos)} titulares nuevos · {len(historico)} en el histórico")

    # La tabla de estado la escribe la WEB (por la API). El build solo la crea la primera vez:
    # si la borrase o la reescribiera, se perderia lo leido.
    if not ESTADO.exists():
        ESTADO.write_text(
            "# Radar IA — estado.  url <TAB> leido|descartado <TAB> fecha\n"
            "# Lo escribe la web al hacer clic. Se puede editar a mano: lo que no aparece aqui,\n"
            "# esta en la bandeja.\n",
            encoding="utf-8",
        )
        print("  estado.tsv creado (vacío)")

    iconos = {f["id"]: f.get("icono", "") for f in fuentes}
    cats_map = {c["id"]: {"emoji": c["emoji"], "nombre": c["nombre"]} for c in cats}
    META.write_text(json.dumps({
        "fuentes": [{"id": f["id"], "nombre": f["nombre"], "idioma": f["idioma"]} for f in fuentes],
        "iconos": iconos,
        "cats": cats_map,
        "repo": cfg["repo"],
        "corte": cfg["corte"],
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
