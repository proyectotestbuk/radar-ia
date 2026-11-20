#!/usr/bin/env python3
"""Radar IA — filtro de morralla con Claude Haiku (desde el 20/11/2026, a petición de Juan).

Lee docs/items.json, toma los titulares posteriores al corte que aún no están en clasificacion.tsv
ni marcados en docs/estado.tsv, y se los pasa a Claude Haiku (claude -p --model haiku) en lotes de 80
con el criterio de prompt-clasifica.txt. Escribe tipo, interés (alto / medio / nada) y motivo en
clasificacion.tsv y los copia a items.json para que la web los vea sin esperar al build del día siguiente.

  python3 clasifica.py            # clasifica lo pendiente
  python3 clasifica.py --aplica   # solo copia clasificacion.tsv a items.json

Lo corre el servidor Debian de Juan cada mañana (timer radar-clasifica), después de la cosecha de GitHub.
"""
import json, re, subprocess, sys
from pathlib import Path

RAIZ = Path(__file__).parent
ITEMS, ESTADO, CLASIF = RAIZ / "docs/items.json", RAIZ / "docs/estado.tsv", RAIZ / "clasificacion.tsv"
PROMPT = (RAIZ / "prompt-clasifica.txt").read_text(encoding="utf-8")
LOTE = 80
CAB = "# Radar IA — clasificación por Claude Haiku.  url <TAB> tipo <TAB> interes (alto|medio|nada) <TAB> motivo\n"


def corte():
    for l in (RAIZ / "config.txt").read_text(encoding="utf-8").splitlines():
        if l.strip().startswith("corte"):
            return l.split("=", 1)[1].strip()
    return "0000"


def leer_tsv(p):
    if not p.exists():
        return {}
    return {l.split("\t")[0]: l.rstrip("\n").split("\t") for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")}


def aplica(items, clasif):
    n = 0
    for i in items:
        c = clasif.get(i["url"])
        if c and len(c) >= 3:
            i["cat"], i["interes"] = c[1], c[2]
            i["motivo"] = c[3] if len(c) > 3 else ""
            n += 1
    ITEMS.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    return n


def main():
    items = json.loads(ITEMS.read_text(encoding="utf-8"))
    clasif = leer_tsv(CLASIF)
    if "--aplica" not in sys.argv:
        vistos = leer_tsv(ESTADO)
        tipos = {l.split("|")[0].strip() for l in (RAIZ / "categorias.txt").read_text(encoding="utf-8").splitlines()
                 if "|" in l and not l.startswith("#")} | {"otros"}
        pend = [i for i in items if i.get("dia", i["fecha"][:10]) >= corte() and i["url"] not in clasif and i["url"] not in vistos]
        print(f"pendientes: {len(pend)}", flush=True)
        if not CLASIF.exists():
            CLASIF.write_text(CAB, encoding="utf-8")
        for k in range(0, len(pend), LOTE):
            lote = pend[k:k + LOTE]
            lista = "\n".join(f"{n}\t{i['fuente']}\t{i['titulo']}" for n, i in enumerate(lote))
            r = subprocess.run(["claude", "-p", "--model", "haiku", PROMPT + "\n\nTITULARES (n\tfuente\ttitular):\n" + lista],
                               capture_output=True, text=True, timeout=900)
            nuevas = []
            for ln in r.stdout.splitlines():
                m = re.match(r"^(\d+)\t([a-z-]+)\t(alto|medio|nada)\t?(.*)$", ln.strip())
                if m and int(m[1]) < len(lote) and m[2] in tipos:
                    u = lote[int(m[1])]["url"]
                    clasif[u] = [u, m[2], m[3], m[4].replace("\t", " ").strip()]
                    nuevas.append("\t".join(clasif[u]))
            with CLASIF.open("a", encoding="utf-8") as f:
                f.write("".join(x + "\n" for x in nuevas))
            print(f"lote {k // LOTE + 1}: {len(nuevas)}/{len(lote)}", flush=True)
    print("aplicadas:", aplica(items, clasif))


if __name__ == "__main__":
    main()
