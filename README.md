# Radar IA

Una página web con los **titulares de IA del día**, generada sola, todas las mañanas.

**Web:** https://proyectotestbuk.github.io/radar-ia/ *(rellenar tras activar Pages)*

## La idea

No es un curador ni un resumidor: es un **agregador de titulares**. Trae el titular literal,
su fuente y su enlace. **Nadie interpreta nada** — decide quien lo lee.

Funciona como una **bandeja de entrada**:

- **Bandeja** (`index.html`) — lo que queda por leer, sea de hoy o de hace cuatro días.
  **Abrir un titular = leerlo**: desaparece de la bandeja y pasa a *Leídos*.
  La **✕** lo descarta sin abrirlo (y desde *Descartados* se puede devolver con **↺**).
- **Filtros** — chips de **fuente** (con su favicon) y de **categoría**, con contadores. Se pulsan para apagar/encender.
- **Archivo** — una página por día, inmutable. Nada se pierde.

### Dónde se guarda lo leído

En el **`localStorage` del navegador**. Es la única opción sin servidor, y tiene un precio que hay que saber:
**el estado es por navegador**. Lo leído en el PC del trabajo no aparece leído en el móvil ni en casa.
Para eso están los botones **Exportar / Importar** del pie.

## Categorías

Las define Juan en `categorias.txt` (clave · emoji · nombre · palabras). El **script etiqueta al cosechar**,
no el navegador. Se aplica la primera regla que casa, así que **el orden del fichero es la prioridad**.
Al cambiar una regla, el siguiente build **reetiqueta todo el histórico**.

## Iconos de las fuentes

`build.py` descarga el favicon real de cada fuente **una sola vez** y lo guarda en `docs/iconos/`.
No se pide nada a terceros al abrir la página (ni rastreo, ni latencia). Sin favicon → iniciales.

## Cómo funciona

```
feeds.txt   →   build.py   →   docs/*.html  →  GitHub Pages
(fuentes)      (cosecha)       (la web)         (publica)
                   ↑
          GitHub Actions, cron diario 07:00
```

Cada mañana, GitHub ejecuta `build.py` en sus servidores: descarga los RSS, descarta lo que ya
había visto (la URL es la identidad), escribe la página del día y la commitea. Pages la publica sola.
**No hace falta que ningún ordenador esté encendido.**

## Coste

**0 €.** GitHub Actions y Pages son gratis en repos públicos. `build.py` **no usa ningún LLM**:
solo biblioteca estándar de Python. No consume tokens ni crédito de API de nadie.

## Añadir o quitar una fuente

Editar `feeds.txt`, una línea por fuente:

```
Nombre visible | es | https://url-del-feed.xml | filtro,opcional,por,palabras
```

El filtro solo hace falta en fuentes generalistas (Genbeta, Microsiervos), para que no entre
lo que no es de IA. Si un feed se cae, el build no revienta: lo salta y lo avisa en el pie de la web.

## Dos diques contra el ruido

Se aprendieron a golpes en la primera prueba, cuando el feed de Hugging Face coló **823 titulares**
de todo su archivo histórico:

- `VENTANA_DIAS = 7` — nada más viejo entra, aunque el feed lo sirva.
- `TOPE_FUENTE = 25` — ninguna fuente puede inundar un día ella sola.

## Probar en local

```bash
py build.py          # regenera docs/ con la cosecha de hoy
```

## Fuentes (verificadas el 14/07/2026)

Español: La Hora Maker (YouTube) · Xataka IA · Genbeta · Microsiervos.
Inglés: r/LocalLLaMA · Simon Willison · Hugging Face Blog · Hacker News (IA) · releases de Ollama y vLLM.

Descartada: *IA en Español* (newsletter.iatoday.ai) — su `/feed` devuelve HTML, no RSS.
