# Deportick explore y watch

Instalación: `python3 -m pip install -r requirements.txt` y `python3 -m playwright install chromium`.

Inicio: `python main.py explore`.

El perfil persistente se guarda en `.playwright-profile/`. Si ya tenés uno,
indicá su ruta con `DEPORTICK_PROFILE=/ruta/al/perfil python main.py explore`.
No abras el mismo perfil en otra instancia de Chromium al mismo tiempo.

Cada inspección guarda captura, HTML y elementos visibles en `artifacts/step_NN/`.
El navegador permanece abierto hasta ingresar `q`. Las acciones requieren
selección y confirmación literal `CONFIRMAR`; CAPTCHA, login y cola se resuelven
manualmente en el navegador.

Los selectores se escriben en `artifacts/discovered_selectors.json` únicamente
después de que un selector único y visible haya funcionado. Un modo normal futuro
puede leerlos con `load_discovered_selectors()` y debe volver a validarlos contra
el DOM antes de actuar. `watch` hace esa validación.

## Watch

Iniciar: `python3 main.py watch`.

Probar otro evento sin editar código:

```bash
python3 main.py watch --keywords "Nombre Equipo 1,Nombre Equipo 2"
python3 main.py watch --url "https://www.deportick.com/event/ejemplo"
```

También acepta `EVENT_URL` y `EVENT_KEYWORDS` (términos separados por comas)
como variables de entorno. La prioridad es `--url`, `--keywords`, `EVENT_URL`,
`EVENT_KEYWORDS` y, finalmente, Argentina/Benín. Con palabras clave se exige
que todos los términos aparezcan en el texto visible del enlace o su tarjeta.

Configuración opcional por variables de entorno:

- `EVENT_URL`: URL exacta del evento, si se conoce.
- `EVENT_KEYWORDS`: términos del evento separados por comas.
- `POLL_INTERVAL`: intervalo de discovery en HOME (mínimo 15; predeterminado 30). La detección de CAPTCHA usa observación corta independiente.
- `RELOAD_INTERVAL`: segundos entre recargas mientras la venta no empezó (mínimo 300; predeterminado 600).
- `DEPORTICK_PROFILE`: ruta del perfil persistente (predeterminado `.playwright-profile`).

El navegador es visible (`HEADLESS=false`). Las palabras objetivo son Argentina y
Benín/Benin (se requieren ambos nombres, con o sin tilde). `watch` sólo hace clic
en un enlace de evento o una acción de compra si el elemento es único y visible.
En HOME escribe `artifacts/home_candidates.json` con cada enlace de evento,
texto, href, selector, coincidencia por término, imágenes, iframes y dimensiones
de la página. Si no aparece el evento y el DOM no cambia, recarga HOME como máximo
una vez cada `RELOAD_INTERVAL` segundos más un jitter de hasta 15 segundos.
No selecciona localidad, cantidad, datos ni pago. Durante la fila sólo observa y
registra URL, estado, señal de clasificación, selector de cada navegación y tiempo
en `artifacts/watch.log`. Al detectar selección de entradas, emite
una alerta sonora, imprime `TURNO DISPONIBLE - CONTINUAR MANUALMENTE` y deja de
interactuar con la página. Si el estado es desconocido, guarda evidencia en
`artifacts/unknown_*/` y también se detiene. Cerrá el proceso con Ctrl+C cuando
termines manualmente.

Simulación sin navegador: `python3 main.py watch --simulate ruta/a/estados.json`.
El JSON debe contener una lista de instantáneas con el mismo esquema que
`elements.json`; imprime la secuencia de estados detectados.

## Venta con fila virtual

Configurá la hora con offset y abrí el navegador antes de la venta:

```bash
SALE_TIME=2026-09-22T18:00:00-03:00 python3 main.py watch --presale
```

`--presale` empieza en HOME y hace OCR local sobre una captura de cada card de
evento. Exige Argentina y Benín completos en el texto visual del **mismo card**;
el `href`, imagen y texto DOM aportan contexto, pero no bastan para confirmar al
rival. Los resultados OCR se guardan en memoria por firma de `href`, `src` y
dimensiones. Un observador del DOM despierta el análisis cuando aparece un nuevo
card o cambia su imagen. No abre eventos ambiguos para averiguar el rival.
Al confirmar el card de Argentina–Benín, hace clic directamente en su anchor y
deja de inspeccionar HOME. El target confirmado se guarda en
`artifacts/target_event.json`. Cuando existe ese archivo, la próxima ejecución
va directamente a esa URL, salvo que la pestaña persistente ya esté en una fila
o selección. El archivo incluye URL, texto verificado, timestamp con microsegundos
y evidencia de la confirmación. Si la fila aparece en el target ya confirmado,
mantiene esa pestaña abierta sin recargar ni volver a HOME.

Para validar el OCR sin clics:

```bash
python3 main.py visual-scan
```

Este modo imprime `href`, imagen, texto OCR y coincidencias de cada card; guarda
sólo capturas de los cards en `artifacts/visual_scan/`. Usa Apple Vision local y
compila `ocr_card.swift` en `artifacts/ocr_card` la primera vez.
Puede entrar a una sala de espera oficial antes de `SALE_TIME`. Mientras está en
fila sólo observa; no recarga ni interactúa con ella. En HOME, las recargas se
separan al menos `RELOAD_INTERVAL` segundos (600 por defecto) más un jitter de
hasta 15 segundos. Los intervalos usan reloj monotónico.

Al confirmar el target empieza HOT PATH: conserva la pestaña del evento y deja
de enumerar HOME, abrir candidatos y guardar capturas preventivas. Un observador
del DOM despierta el proceso al aparecer cambios; una comprobación de respaldo
ocurre cada 10 segundos. El clic en una acción de entrada inequívoca ocurre antes
de escribir logs o evidencia. El log registra timestamps con microsegundos para
`TARGET EVENT VERIFIED`, `HOT PATH ENTERED`, `BUY ACTION APPEARED`,
`BUY ACTION CLICKED`, `QUEUE ENTERED` y `TICKET_SELECTION`; el clic incluye
`reaction_ms`. Al faltar 10 minutos, `caffeinate` mantiene despierta la máquina
sin navegar ni recargar. Capturas, HTML y DOM se guardan sólo si hay error o
estado desconocido. En selección se detienen las interacciones y suena la alarma.

Una URL confirmada se conserva en `artifacts/target_event.json`; `QUEUE_MODE`
indica `NONE`, `UNVERIFIED` o `TARGET_VERIFIED` en el log.
Los logs distinguen `AUTO NAVIGATION` y `MANUAL/EXTERNAL NAVIGATION`. Una llegada
manual a selección queda marcada `NO AUTO SUCCESS`.

Comprobaciones previas:

```bash
python3 main.py preflight
python3 main.py alarm-test
```

`preflight` abre Chromium visible, comprueba la carga de Deportick, escribe un
informe y captura en `artifacts/preflight*`, y prueba el sonido. Si aparece el
enlace de ingreso, informa `LOGIN_REQUIRED`; el login y la audición de la alarma
requieren comprobación humana. Cerrá otros procesos que usen el mismo perfil
antes de ejecutarlo.

### Diagnóstico de watch

```bash
SALE_TIME=2026-09-22T18:00:00-03:00 python3 main.py watch --presale --debug-watch
```

Muestra en cada ciclo URL, estado, enlaces de evento, candidatos de Argentina,
rechazados, próximo reload y tiempo restante. Registra `HOME SCAN`,
`NEW EVENT LINK DETECTED` y `HOME RELOAD`. Sólo en este modo, HOME puede recargarse
cada 30 segundos; la fila, el evento verificado y los estados de error nunca se
recargan por debug. En la terminal: `r` + Enter solicita un reload de HOME cuando
se cumplió el intervalo; `i` + Enter imprime los candidatos; `q` + Enter sale.
Sin `--debug-watch` se mantienen los intervalos conservadores normales.


### CAPTCHA durante `watch` y `watch --presale`

El HOT PATH detecta challenges visibles (reCAPTCHA, hCaptcha, Turnstile y
textos de verificación), entra en `CAPTCHA_REQUIRED`, trae la pestaña al frente
y emite una alarma repetida. Resolvé el CAPTCHA manualmente en esa misma
pestaña: no hace falta presionar ENTER. Mientras está presente, solamente
observa DOM/URL, sin recargar, navegar ni interactuar con el challenge.

Al desaparecer, apaga la alarma y retoma automáticamente. Las transiciones
`CAPTCHA_DETECTED`, `CAPTCHA_CLEARED` y `QUEUE_ENTERED` quedan registradas con
timestamps. La fila del target confirmado conserva `QUEUE_MODE=TARGET_VERIFIED`.
No captura pantalla/HTML ni ejecuta discovery durante la reanudación; una
redirección todavía desconocida se observa pasivamente hasta un estado seguro.

Prueba local, sin contactar Deportick ni resolver un CAPTCHA real:

```bash
python3 main.py captcha-test
```

Abre un navegador temporal con contenido sintético, reproduce
`TARGET VERIFIED → CAPTCHA_REQUIRED → CAPTCHA CLEARED → QUEUE` y lo cierra
al terminar. La alarma usa el sonido del sistema y la campana del terminal.

La prioridad de clasificación es CAPTCHA → login → error → selección → fila.
Una URL `deportick.queue-it.net` con “No soy un robot” sigue siendo
`CAPTCHA_REQUIRED`; recién sin señales de challenge se clasifica como `QUEUE`.
Ambos modos observan cambios del DOM y vuelven a comprobar a intervalos cortos
(100 ms en el watch normal y durante CAPTCHA), incluyendo iframes visibles.
Después de un click, una única espera observa todos los estados seguros en
paralelo lógico; no usa el polling general de 30 segundos. La nueva versión se
aplica a procesos iniciados después del cambio.


## Desarrollo y uso compartido

Python 3.9 o posterior. Las alertas nativas y el OCR de preventa están orientados
a macOS; el OCR requiere Swift/Command Line Tools y Apple Vision.

Después de clonar el repositorio:

```bash
cd snipeBot
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements-dev.txt
python3 -m playwright install chromium
python3 -m unittest -q
python3 -m ruff check main.py test_main.py
python3 main.py watch --url "https://www.deportick.com/event/ejemplo"
```

Cada persona debe iniciar sesión por su cuenta en el navegador. El repositorio
excluye perfiles del navegador, cookies, archivos `.env`, logs, capturas, HTML,
estado de eventos y exportaciones ZIP. No agregues esos archivos con `git add -f`.
Los CAPTCHA se resuelven exclusivamente a mano en la pestaña abierta.
