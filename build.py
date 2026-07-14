#!/usr/bin/env python3
"""Radar IA — agregador de titulares.

Lee los feeds de feeds.txt, acumula los titulares en web/items.json y genera
una pagina por dia, el indice, el archivo historico y el buscador.

Sin dependencias: solo biblioteca estandar (urllib + xml.etree).
Se ejecuta solo, cada manana, en GitHub Actions.
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

RAIZ = Path(__file__).parent
WEB = RAIZ / "docs"          # GitHub Pages publica esta carpeta tal cual
ITEMS = WEB / "items.json"

# Reddit y algun otro rechazan el user-agent por defecto de urllib.
UA = "Mozilla/5.0 (compatible; RadarIA/1.0; +https://github.com/proyectotestbuk/radar-ia)"
TIMEOUT = 25
MADRID = timezone(timedelta(hours=2))  # CEST; solo para fechar el dia, no es critico

# Dos diques contra el ruido, aprendidos a base de tragarnos el archivo entero
# de Hugging Face (823 titulares) en la primera prueba:
VENTANA_DIAS = 7   # nada mas viejo que esto entra, aunque el feed lo sirva
TOPE_FUENTE = 25   # ninguna fuente puede inundar un dia

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
}


# ---------------------------------------------------------------- feeds

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
        nombre, idioma, url = partes[0], partes[1], partes[2]
        filtro = partes[3] if len(partes) > 3 else ""
        fuentes.append({
            "nombre": nombre,
            "idioma": idioma,
            "url": url,
            "filtro": [p.strip().lower() for p in filtro.split(",") if p.strip()],
        })
    return fuentes


def descargar(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def texto(nodo):
    if nodo is None:
        return ""
    t = "".join(nodo.itertext()) if len(nodo) else (nodo.text or "")
    return html.unescape(re.sub(r"\s+", " ", t)).strip()


def fecha_iso(bruto):
    """Normaliza cualquier fecha de feed a ISO. Si no se puede, devuelve ''."""
    if not bruto:
        return ""
    bruto = bruto.strip()
    try:
        return parsedate_to_datetime(bruto).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(bruto.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except ValueError:
        return ""


def parsear(xml_bytes, fuente):
    """Extrae items de un feed RSS 2.0 o Atom."""
    raiz = ET.fromstring(xml_bytes)
    items = []

    # RSS 2.0
    for it in raiz.iter("item"):
        titulo = texto(it.find("title"))
        enlace = texto(it.find("link"))
        fecha = fecha_iso(texto(it.find("pubDate")) or texto(it.find("dc:date", NS)))
        if titulo and enlace:
            items.append((titulo, enlace, fecha))

    # Atom (YouTube, GitHub releases, Reddit, Simon Willison...)
    for it in raiz.iter(f"{{{NS['atom']}}}entry"):
        titulo = texto(it.find("atom:title", NS))
        enlace_nodo = it.find("atom:link[@rel='alternate']", NS) or it.find("atom:link", NS)
        enlace = enlace_nodo.get("href", "") if enlace_nodo is not None else ""
        fecha = fecha_iso(texto(it.find("atom:published", NS)) or texto(it.find("atom:updated", NS)))
        if titulo and enlace:
            items.append((titulo, enlace, fecha))

    return [
        {
            "titulo": t,
            "url": u,
            "fecha": f,
            "fuente": fuente["nombre"],
            "idioma": fuente["idioma"],
        }
        for t, u, f in items
    ]


def es_reciente(item, limite):
    """Sin fecha, se acepta: la dedup por URL ya evita repetirlo."""
    if not item["fecha"]:
        return True
    try:
        return datetime.fromisoformat(item["fecha"]) >= limite
    except ValueError:
        return True


def pasa_filtro(item, filtro):
    if not filtro:
        return True
    t = item["titulo"].lower()
    return any(p in t for p in filtro)


def cosechar(fuentes):
    nuevos, caidos = [], []
    limite = datetime.now(timezone.utc) - timedelta(days=VENTANA_DIAS)

    for f in fuentes:
        try:
            items = parsear(descargar(f["url"]), f)
        except (urllib.error.URLError, urllib.error.HTTPError, ET.ParseError, TimeoutError, OSError) as e:
            print(f"  FALLO    {f['nombre']}: {type(e).__name__} {e}")
            caidos.append(f["nombre"])
            continue

        brutos = len(items)
        items = [i for i in items if es_reciente(i, limite) and pasa_filtro(i, f["filtro"])]
        items = items[:TOPE_FUENTE]
        descartados = brutos - len(items)
        nota = f"  (-{descartados} viejos/filtrados)" if descartados else ""
        print(f"  ok  {len(items):3d}  {f['nombre']}{nota}")
        nuevos.extend(items)

    return nuevos, caidos


# ---------------------------------------------------------------- historico

def fusionar(cosecha, hoy):
    """Anade al historico lo que no estuviera ya. La URL es la identidad."""
    historico = json.loads(ITEMS.read_text(encoding="utf-8")) if ITEMS.exists() else []
    vistas = {i["url"] for i in historico}

    frescos = []
    for item in cosecha:
        if item["url"] in vistas:
            continue
        vistas.add(item["url"])
        item["dia"] = hoy  # el dia en que ESTE radar lo vio por primera vez
        frescos.append(item)

    historico = frescos + historico
    ITEMS.write_text(json.dumps(historico, ensure_ascii=False, indent=1), encoding="utf-8")
    return frescos, historico


# ---------------------------------------------------------------- html

CSS = """
:root{--bg:#fbfaf8;--fg:#1a1a19;--dim:#6b6a66;--linea:#e5e2dc;--card:#fff;--ac:#b4530a}
@media(prefers-color-scheme:dark){:root{--bg:#16161a;--fg:#e8e6e1;--dim:#918f89;--linea:#2c2c33;--card:#1d1d22;--ac:#e8945a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 -apple-system,"Segoe UI",system-ui,sans-serif;
 -webkit-font-smoothing:antialiased}
.wrap{max-width:820px;margin:0 auto;padding:28px 20px 80px}
header{border-bottom:2px solid var(--fg);padding-bottom:14px;margin-bottom:26px}
h1{font-size:1.5rem;margin:0 0 4px;letter-spacing:-.02em}
h1 span{color:var(--ac)}
.sub{color:var(--dim);font-size:.86rem}
nav{margin-top:14px;display:flex;gap:16px;flex-wrap:wrap;font-size:.86rem}
nav a{color:var(--ac);text-decoration:none;border-bottom:1px solid transparent}
nav a:hover{border-color:var(--ac)}
h2{font-size:.78rem;text-transform:uppercase;letter-spacing:.09em;color:var(--dim);
 margin:30px 0 10px;font-weight:600}
ul{list-style:none;margin:0;padding:0}
li{background:var(--card);border:1px solid var(--linea);border-radius:9px;padding:12px 14px;margin-bottom:8px}
li:hover{border-color:var(--ac)}
a.t{color:var(--fg);text-decoration:none;font-weight:500;display:block;margin-bottom:5px}
a.t:hover{color:var(--ac)}
.meta{font-size:.75rem;color:var(--dim);display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.badge{border:1px solid var(--linea);border-radius:20px;padding:1px 8px;font-size:.7rem}
.vacio{color:var(--dim);font-style:italic;padding:20px 0}
input[type=search]{width:100%;padding:12px 14px;font-size:1rem;border:1px solid var(--linea);
 border-radius:9px;background:var(--card);color:var(--fg);margin-bottom:8px}
input[type=search]:focus{outline:2px solid var(--ac);border-color:transparent}
footer{margin-top:50px;padding-top:16px;border-top:1px solid var(--linea);color:var(--dim);font-size:.78rem}
"""

CABECERA = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{titulo}</title><style>{css}</style></head><body><div class="wrap">
<header><h1>Radar <span>IA</span></h1>
<div class="sub">{sub}</div>
<nav><a href="index.html">Hoy</a><a href="buscar.html">Buscar</a><a href="archivo.html">Archivo</a></nav>
</header>
"""

PIE = """<footer>Titulares tal cual llegan de la fuente — sin resumir, sin filtrar, sin IA de por medio.
Generado por <code>build.py</code> el {sello}. {caidos}</footer></div></body></html>"""


def bandera(idioma):
    return "🇬🇧" if idioma == "en" else "🇪🇸"


def li(item):
    return (
        f'<li><a class="t" href="{html.escape(item["url"])}" target="_blank" rel="noopener">'
        f'{html.escape(item["titulo"])}</a>'
        f'<div class="meta"><span class="badge">{html.escape(item["fuente"])}</span>'
        f'<span>{bandera(item["idioma"])}</span></div></li>'
    )


def pagina_dia(items, dia, caidos, sello):
    por_fuente = {}
    for i in items:
        por_fuente.setdefault(i["fuente"], []).append(i)

    cuerpo = []
    if not items:
        cuerpo.append('<p class="vacio">Hoy no hay titulares nuevos. Pasa: no todos los días se mueve algo.</p>')
    for fuente, grupo in por_fuente.items():
        cuerpo.append(f"<h2>{html.escape(fuente)}</h2><ul>")
        cuerpo.extend(li(i) for i in grupo)
        cuerpo.append("</ul>")

    sub = f"{len(items)} titulares nuevos · {dia}"
    aviso = f"⚠️ Feeds caídos hoy: {', '.join(caidos)}." if caidos else ""
    return (
        CABECERA.format(titulo=f"Radar IA — {dia}", css=CSS, sub=sub)
        + "\n".join(cuerpo)
        + PIE.format(sello=sello, caidos=aviso)
    )


def pagina_archivo(historico, sello):
    dias = {}
    for i in historico:
        dias[i["dia"]] = dias.get(i["dia"], 0) + 1

    filas = "\n".join(
        f'<li><a class="t" href="{d}.html">{d}</a>'
        f'<div class="meta"><span class="badge">{n} titulares</span></div></li>'
        for d, n in sorted(dias.items(), reverse=True)
    )
    return (
        CABECERA.format(titulo="Radar IA — Archivo", css=CSS, sub=f"{len(dias)} días · {len(historico)} titulares")
        + f"<h2>Histórico</h2><ul>{filas}</ul>"
        + PIE.format(sello=sello, caidos="")
    )


BUSCADOR_JS = """
<h2>Buscar en todo el histórico</h2>
<input type="search" id="q" placeholder="ollama, vLLM, pgvector, RAG, licencia..." autofocus>
<div class="meta" id="n"></div><ul id="r"></ul>
<script>
let D=[];
fetch('items.json').then(r=>r.json()).then(d=>{D=d;document.getElementById('n').textContent=D.length+' titulares indexados';});
const bandera=l=>l==='en'?'\\uD83C\\uDDEC\\uD83C\\uDDE7':'\\uD83C\\uDDEA\\uD83C\\uDDF8';
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
document.getElementById('q').addEventListener('input',e=>{
  const q=e.target.value.toLowerCase().trim();
  const r=document.getElementById('r');
  if(q.length<2){r.innerHTML='';document.getElementById('n').textContent=D.length+' titulares indexados';return;}
  const h=D.filter(i=>i.titulo.toLowerCase().includes(q)||i.fuente.toLowerCase().includes(q)).slice(0,80);
  document.getElementById('n').textContent=h.length+' resultados';
  r.innerHTML=h.map(i=>`<li><a class="t" href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.titulo)}</a>
    <div class="meta"><span class="badge">${esc(i.fuente)}</span><span>${bandera(i.idioma)}</span>
    <span>${i.dia}</span></div></li>`).join('');
});
</script>
"""


def main():
    ahora = datetime.now(MADRID)
    hoy = ahora.strftime("%Y-%m-%d")
    sello = ahora.strftime("%d/%m/%Y a las %H:%M")
    WEB.mkdir(exist_ok=True)

    print(f"Radar IA — cosecha del {hoy}")
    fuentes = leer_feeds()
    cosecha, caidos = cosechar(fuentes)
    frescos, historico = fusionar(cosecha, hoy)
    print(f"\n  {len(frescos)} titulares nuevos · {len(historico)} en el histórico")

    del_dia = [i for i in historico if i["dia"] == hoy]
    pagina = pagina_dia(del_dia, hoy, caidos, sello)
    (WEB / f"{hoy}.html").write_text(pagina, encoding="utf-8")
    (WEB / "index.html").write_text(pagina, encoding="utf-8")
    (WEB / "archivo.html").write_text(pagina_archivo(historico, sello), encoding="utf-8")
    (WEB / "buscar.html").write_text(
        CABECERA.format(titulo="Radar IA — Buscar", css=CSS, sub="Todo lo cosechado desde el primer día")
        + BUSCADOR_JS
        + PIE.format(sello=sello, caidos=""),
        encoding="utf-8",
    )

    if len(caidos) == len(fuentes):
        print("\n  TODOS los feeds han fallado — algo va mal (¿red?).")
        return 1
    print(f"  docs/ generada. Feeds caídos: {caidos or 'ninguno'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
