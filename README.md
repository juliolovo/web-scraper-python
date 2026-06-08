# Instrucciones para ejecutar el scraper de productos de hardware

## Requisitos

- Python 3.7 o superior
- Instalar dependencias:
  - requests
  - beautifulsoup4
  - playwright

Puedes instalar las dependencias ejecutando:

```ps
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:PLAYWRIGHT_BROWSERS_PATH = ".venv\ms-playwright"
.\.venv\Scripts\python.exe -m playwright install chromium
```

## Configuración

1. Crea o edita el archivo `config.json` siguiendo el ejemplo incluido.
2. Asegúrate de que los selectores CSS y URLs sean correctos para la marca y las páginas que deseas scrapear.

Estructura importante del `config.json` actualizado:

- `master_page`: configuración compartida para todas las URLs (selectores, tipo de página, etc.).
- `links`: lista de URLs/categorías a scrapear reutilizando `master_page`.
  - Puede ser string (`"https://sitio/categoria"`) o objeto (`{"category": "...", "url": "..."}`).
  - Cada link puede sobrescribir partes de `master_page` si lo necesitas.
- Los selectores aceptan string o arreglos (fallback en orden): `selector` o `selectors`.
- `output.save_per_link_files`: si es `true`, guarda un JSON por link/categoría.
- `output.save_combined_file`: si es `true`, también guarda el archivo combinado de toda la marca.
- `output.root_dir`: raíz de salida depurada (por defecto `output`).
- `output.media_root_dir`: raíz para archivos descargados (por defecto `media`).
- `output.logs_root_dir`: raíz para logs JSON de inspección/ejecución (por defecto `logs_execution`).

## Ejecución

Desde la terminal, ejecuta:

```ps
.\.venv\Scripts\python.exe main.py --config config.json
```

- El scraper inspeccionará cada página definida en `links` y luego cada página detalle de producto.
- El JSON depurado se guarda en `output/data/{brand}/{brand}.json` con `brand` y `products`.
- Las imágenes descargadas se guardan en `media/images/{brand}/`.
- Los documentos, videos y enlaces externos se registran como referencias, pero por defecto no se descargan.
- La trazabilidad de lectura y assets se guarda en `logs_execution/{brand}-log-execution.json`.
- Para validar sólo una muestra puedes usar `--limit-products 1`.

## Notas

- El scraper está diseñado para ser fácilmente extensible y mantenible.
- Si necesitas scrapear otra marca, crea un nuevo archivo de configuración siguiendo la misma estructura.
- El flujo intenta extraer datos en este orden: fuentes estructuradas/API descubiertas desde el HTML, HTML estático con BeautifulSoup y, si la config marca `requires_rendered_dom`, Playwright como fallback opcional.
- La salida de productos usa campos comunes depurados (`brand`, `manufacturer_category`, `type`, `name`, `model`, `url`, `images`, `documents`, `video`, `features`, `specs`, `variants`, `related_products`). Los datos de inspección y origen quedan en `logs_execution/`.
- Para usar el fallback de Playwright instala sus navegadores dentro del proyecto usando `PLAYWRIGHT_BROWSERS_PATH=.venv\ms-playwright`.
## Current Output Contract

Scraper runs write clean product data to `output/data/{brand}/{brand}.json`.
Execution traces are overwritten in `logs_execution/{brand}-log-execution.json`.
The execution log uses `assets.images`, `assets.documents`, `assets.video`, and
`assets.links`; do not use `assets.media`. Only images are downloaded into
`media/images` for now. Videos, documents, and external links are recorded as
source URLs unless a future task explicitly enables downloading them.
