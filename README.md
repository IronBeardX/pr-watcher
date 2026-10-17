# pr-watcher

Vigila los pull requests de Bitbucket hacia `main` **sin gastar tokens** y solo despierta a Claude
cuando hay trabajo real: un PR nuevo, commits nuevos en uno ya visto, o un draft que pasa a ready.

```
timer systemd (cada N s) ──► prwatch tick ──► API de Bitbucket (solo lectura, 0 tokens)
                                   │
                                   └─ ¿hay trabajo? ──► claude -p "…" ──► comentario + voto en el PR
                                                                                    + respuesta en el hilo de Slack
```

Claude **revisa el PR** como lo hace el equipo y lo publica como tú:

1. Un comentario en el PR con la revisión.
2. *Request changes* si hay hallazgos, o *Approve* solo cuando no queda nada abierto.
3. La misma revisión en el hilo del anuncio del PR en el canal de PRs de Slack.
4. Las reacciones del canal (👀 al empezar, ⚠️ o ✅ al terminar).

Con *Publicar en Bitbucket* apagado (Ajustes), solo te manda la revisión por DM. La revisión sigue la skill
`personal-pr-review` (ver más abajo).

## Contexto y estado del proyecto

Empezó como una herramienta personal para un trabajo concreto: un proyecto de cliente que usaba Bitbucket,
Jira y Slack, donde revisaba los PRs de mis compañeros. Ahora lo estoy convirtiendo en una herramienta
general para cualquier proyecto en el que participe. Por ahora sigue dando por hecho Bitbucket, el conector
MCP de Atlassian y el de Slack.

Antes de publicarlo lo limpié de todo lo que lo ataba a ese trabajo:

- **Valores por defecto vacíos**: workspace, repo, canal, proyectos de Jira y carpeta del proyecto. Antes
  venían fijados en el código, y por eso el asistente no los preguntaba. Ahora los pide, y la configuración
  no se da por terminada hasta tener workspace, repo y carpeta (y el canal, si se publica en el hilo).
- **La rúbrica, convertida en plantilla**: estaba escrita para un único repo. Ahora las
  secciones de convenciones y de trampas del repo (§3 y §4) son ejemplos que cada equipo sustituye por las
  suyas.
- **Tests con datos ficticios** (`acme/webapp`, `ABC-123`) y uno nuevo para la validación de la
  configuración.

**Por qué es un agente y no un script.** El watcher no le dice a Claude qué pasos seguir: le da un objetivo
(revisar el PR y publicar la revisión) y un conjunto cerrado de herramientas. Claude decide qué leer y en
qué orden, en un bucle de hasta 40 turnos, y actúa sobre sistemas reales. Lo que se le permite hacer no
depende del modelo: lo valida un hook `PreToolUse` antes de cada llamada (ver *Seguridad*). El bucle en sí
lo pone Claude Code en modo headless (`claude -p`). Este proyecto aporta la orquestación, los permisos, el
guardián y la recuperación ante fallos.

## Revisión de PRs

- **Qué lee.** Claude (modelo `opus`, hasta 40 turnos y 20 min) lee el PR con el conector de Atlassian:
  metadatos, diff, comentarios de otros revisores, archivos del repo, CI del commit (también pipelines) y
  AC del ticket de Jira. Lo juzga con la rúbrica de la skill.
- **Qué publica, en este orden.**
  1. Busca el anuncio del PR en el canal: el mensaje más reciente que enlaza
     `https://bitbucket.org/<workspace>/<repo>/pull-requests/<n>`. Lo abre con `slack_read_thread`, lo que
     también le deja ver qué dijeron otros revisores.
  2. Comenta en el PR. La primera línea es `**Verdict:** Approve — 0 findings` o
     `**Verdict:** Request Changes — <n> findings`.
  3. Marca el voto en el PR.
  4. Responde en el hilo del anuncio con una primera línea como la del equipo
     (`:warning: _#3962_ (…) — _changes requested_ (marked on Bitbucket).`) y el mismo texto.
  5. Reacciona en el anuncio.

  Si no encuentra el anuncio, te lo cuenta por DM.
- **A medias = fallo, y se completa.** Si un intento publica el comentario pero falta el voto o el mensaje
  de Slack, cuenta como fallido (`falló 1/2`), y el siguiente intento con el mismo commit solo hace lo que
  faltaba. Lo mismo si el proceso muere a mitad: lo ya publicado se recupera del rastro. El guardián no deja
  publicar dos veces el comentario ni el hilo.
- **Veredicto.** *Approve* solo con cero hallazgos. Cualquier hallazgo, por pequeño que sea, es *Request
  Changes*. Cada hallazgo lleva su sección: `F1 [tests] …`, `F2 [conventions] …`.
- **Re-revisión.** Si el PR recibe commits nuevos, se vuelve a revisar. Claude recibe los hallazgos abiertos
  de la revisión anterior y el commit desde el que mirar, y dice para cada uno si está resuelto. Si todo
  sigue igual y no hay nada nuevo, **no publica nada** (en la tabla sale `sin cambios`). Solo aprueba
  cuando todos los hallazgos están resueltos.
- **CI.** El watcher pasa a Claude un resumen de los checks del commit (en verde, en curso, fallidos), sacado
  de la API de Bitbucket, así que eso no gasta tokens.
- **Tabla.** La columna *Último envío* muestra `✓ aprobar`, `✓ cambios(n)` o `✓ sin cambios`, con `BB` si
  el comentario está en el PR y `!` si algo no se pudo publicar. El detalle (`enter`) muestra los hallazgos
  abiertos, qué se publicó y el enlace al comentario.

### La skill de revisión

La skill vive en este repo, en `skills/personal-pr-review/` (`SKILL.md` + `references/rubric.md`).
Las secciones de convenciones y trampas del repo (§3 y §4 de la rúbrica) son una
plantilla: cámbialas por las reglas de tu equipo. El watcher la busca en
`<carpeta del proyecto>/.claude/skills/<review_skill>/` y la incluye en el prompt. Para instalarla en el repo
basta un enlace simbólico (añade `.claude/skills/personal-*` a su `.gitignore` si no quieres subirla):

```zsh
ln -s /ruta/a/pr-watcher/skills/personal-pr-review \
  /ruta/a/tu-repo/.claude/skills/personal-pr-review
```

El nombre de la skill es configurable (`review_skill` en Ajustes o en el asistente). Las skills personales
(no del equipo) se llaman `personal-…`. Si la skill no existe, el PR queda en `falló` sin llamar a Claude.

La skill solo revisa. Vigilar PRs, decidir cuáles revisar y guardar el estado lo hace el watcher. Claude
manda el DM porque se lo pide el prompt del watcher, no la skill.

### Herramientas que puede usar Claude

Se editan en Ajustes (`c`) → *Herramientas permitidas*, como el PATH de Windows: una por línea, con
*Añadir*, *Reemplazar*, *Quitar*, ↑ ↓ y *Por defecto*. Las de por defecto son:

- Slack: `mcp__claude_ai_Slack__slack_send_message`, `…__slack_read_channel`, `…__slack_read_thread`,
  `…__slack_add_reaction`
- Atlassian: `mcp__claude_ai_Atlassian_MCP__executeRead`, `…__executeWrite`, `…__discover`,
  `…__getJiraIssue`, `…__getAccessibleAtlassianResources`

Si en tu cuenta el conector tiene otro prefijo (míralo con `/mcp` dentro de tu comando de Claude), cámbialos
ahí. Estar en la lista no da permiso para todo: el guardián decide qué puede hacer cada herramienta (ver
*Seguridad*). Una herramienta sin reglas en el guardián (por ejemplo una de Gmail o una búsqueda de Jira) se
bloquea aunque esté en la lista.

## Instalar

```zsh
git clone https://github.com/IronBeardX/pr-watcher.git && cd pr-watcher
uv tool install --editable .       # deja `prwatch` en ~/.local/bin
prwatch                            # abre la TUI
```

Sin `uv`: `python -m venv .venv && .venv/bin/pip install -e . && .venv/bin/prwatch`.

Para desinstalar: `prwatch remove-timer` (para el timer y borra sus units), luego
`uv tool uninstall pr-watcher`. Si además quieres borrar los datos:
`rm -rf ~/.config/pr-watcher ~/.local/state/pr-watcher`.

## Primer arranque: el asistente

La primera vez que ejecutas `prwatch` se abre un asistente de seis pasos:

1. **Inicio:** qué hace el watcher.
2. **Claude:** el comando (por defecto `claude`; puede ser un alias de otra cuenta) y cómo lanzarlo. El modo `zsh-interactive` carga
   tu `~/.zshrc`, así que los alias funcionan. El botón *Comprobar* ejecuta `<comando> --version`, sin gastar
   tokens.
3. **Ventana:** las horas hacia atrás en las que se buscan PRs. El watcher pide a Bitbucket los PRs hacia la
   rama destino con actividad entre *ahora − ventana* y *ahora* (por ejemplo 4, 24 o 168; 0 = todos los
   abiertos). En el mismo paso eliges cada cuántos minutos consulta, lo que tampoco gasta tokens.
4. **Slack:** tu id de miembro (`U…`), que es el único destino al que el guardián deja escribir, y el nombre
   exacto de la herramienta de enviar mensajes (lo ves con `/mcp`).
5. **Proyecto:** la carpeta del repo local, el workspace y el repo de Bitbucket, y los proyectos de Jira cuyos
   tickets puede leer Claude (opcional). De la carpeta se toman `BB_EMAIL` y `BB_TOKEN` (de
   su `.env`) y la skill de revisión. *Comprobar Bitbucket* valida el token sin gastar tokens de Claude. En
   este paso también pones tu nickname de Bitbucket, eliges si saltar tus propios PRs (activado por defecto)
   y das el nombre de la skill de revisión; el asistente comprueba que exista.
6. **Resumen:** un botón opcional para mandar un DM de prueba y un interruptor para activar el timer al
   terminar.

Mientras el asistente no se complete, el timer no hace nada y no se puede activar. Puedes repetirlo cuando
quieras con la tecla `w`, o cambiar valores sueltos en Ajustes (`c`).

### Ventana y primer arranque

Con ventana (por ejemplo 24 h), el primer tick manda a Claude los PRs abiertos con actividad en esa ventana,
como mucho `max_reviews_per_tick` por tick (3 por defecto). Un PR que se queda sin actividad sale de la ventana
pero no se marca cerrado: solo se marca `cerrado` cuando Bitbucket dice que se fusionó o se declinó.

Con ventana 0 se vigilan todos los PRs abiertos, y los que ya estaban abiertos en el primer arranque quedan
como **punto de partida**: no se mandan a Claude salvo que cambien, pasen de draft a ready o los pidas a mano
con `r`.

## Atajos de la TUI

| Tecla | Acción |
|---|---|
| `p` · `P` | Pausar / reanudar · pausar X minutos (se reanuda solo) |
| `t` | Tick manual ahora (funciona también en pausa) |
| `r` · `R` | Revisar ya el PR seleccionado · revisar por número (funciona aunque esté en pausa) |
| `i` · `I` | Ignorar / dejar de ignorar el seleccionado · por número |
| `f` | Solo PRs creados desde una fecha (vacío = sin filtro) |
| `c` · `w` | Ajustes · repetir el asistente |
| `x` | DM de prueba vía Claude |
| `h` | Mostrar u ocultar los cerrados, ignorados y propios |
| `enter` · `o` | Detalle del PR · abrir en el navegador |
| `ctrl+p` | Paleta de comandos (incluye cambiar de tema) |
| `q` | Salir (el watcher sigue corriendo) |

**Pausar** para el timer de systemd de verdad: no se lanza ningún tick automático, y sigue parado aunque
reinicies. Con la pausa puesta, `t` (tick manual) y `r`/`R` siguen funcionando. Con `P` eliges los minutos y
systemd reanuda solo al terminar. Si el PC se reinicia antes, la TUI o `prwatch status` reanudan en cuanto
ven que la hora ya pasó. Para quitar el timer del todo: `prwatch remove-timer`.

Un timer parado o esperando no consume nada. El único coste es cada tick: menos de un segundo de CPU y una
llamada HTTPS.

## CLI (para scripts o alias de zsh)

```zsh
prwatch status | enable | disable | pause [min] | resume | run
prwatch review 3567 | ignore 3567 | unignore 3567
prwatch since 2026-10-01 | prwatch since --clear
prwatch test-claude | prwatch config-path | prwatch remove-timer
prwatch last-run | prwatch logs -n 50
```

## Estados de un PR

`esperando` (pushes recientes; se espera unos minutos para juntarlos) → `pendiente` → `revisando` →
`revisado`, o `falló n/2`. Un PR que falla se reintenta una vez por commit; después espera a un commit
nuevo o a una revisión manual.

Otros estados: `baseline` (estaba abierto en el primer arranque), `draft`, `ignorado`,
`mío`, `autor ignorado`, `anterior a fecha` y `cerrado`.

La línea **Salud** de la TUI (y `prwatch status`) se pone en rojo en estos casos:

- Bitbucket falla.
- El servicio de systemd falla.
- El timer está activo pero no hay ticks recientes.
- Falta configuración.

Los errores repetidos se registran una vez y luego como mucho una vez por hora.

Si revisas a mano un PR que apunta a otra rama, su estado lleva `· →<rama> (no se sigue)`: el escaneo
automático solo mira PRs hacia la rama destino, así que ese PR no se reintenta ni se marca como cerrado.
Se olvida tras 30 días. Un PR que deja de aparecer abierto hacia la rama destino se marca `cerrado` (puede
estar cerrado, fusionado o haber cambiado de rama).

## Archivos

- `~/.config/pr-watcher/config.json`: configuración (también desde `c`).
- `~/.config/pr-watcher/prompt.md`: solo para el DM de prueba (`x`). Admite `{id}`, `{title}`,
  `{author}`, `{url}`, `{reason_text}`, `{source_branch}`, `{destination}`, `{head_short}`, `{slack_user_id}` y
  `{expected_message}` (el único texto de DM que el guardián deja enviar en esta versión).
- `~/.local/state/pr-watcher/`: `state.json`, `events.log` y `runs/` (salida completa de cada
  llamada a Claude, con su coste). `prwatch last-run` muestra la última.
- `~/.config/systemd/user/pr-watcher.{service,timer}`: las units que crea `a`.

## Seguridad

El título, la descripción, el diff y los comentarios de un PR los escribe otra persona, así que el watcher
los trata como no fiables (posible inyección de prompt) y aplica cinco defensas:

1. **Campos del PR aislados.** Se limpian, se recortan y van dentro de un bloque `<pr_data>` marcado como
   datos, no instrucciones.
2. **Lista corta de herramientas.** Claude solo recibe las herramientas que pongas, siempre con su nombre
   exacto (no se aceptan servidores enteros). Bash, Read, Write, WebFetch, etc. quedan prohibidas con
   `--disallowedTools`, y se fuerza `--permission-mode default`.
3. **Guardián.** Un hook `PreToolUse` (`prwatch guard`) revisa cada herramienta antes de que se ejecute.
   Funciona aunque la cuenta tenga permisos amplios, y los bloqueos aparecen en el log. Bloquea:
   - todo lo que no esté en la lista de herramientas;
   - en Bitbucket, cualquier escritura salvo tres en el PR que se está revisando: un comentario general
     (sin comentarios en línea ni borradores), *Request changes* y *Approve*. Nunca mergear, declinar,
     crear tareas, editar o borrar comentarios, ni nada en Jira;
   - el voto antes del comentario, o un voto que no cuadre con la línea `**Verdict:**` del comentario;
   - *Approve* con algún hallazgo abierto o un hallazgo anterior `partially` / `not addressed`;
   - cualquier escritura si el PR ya no está abierto o tiene commits nuevos. Antes de cada escritura lo
     comprueba en Bitbucket con tu token de solo lectura;
   - repetir el comentario o el hilo de un commit que ya se publicó;
   - las lecturas fuera de las operaciones conocidas o de otro repo distinto del configurado, y
     los tickets de Jira de proyectos que no estén en `jira_projects` (vacío = ninguno);
   - cualquier herramienta sin reglas propias en el guardián;
   - menciones y avisos (`<@…>`, `<!channel>`, `<!here>`, `@{…}` en Bitbucket);
   - en Slack, leer otro canal que no sea el de PRs. Escribir en algo que no sea el hilo del anuncio de
     ese PR o tu propio DM. Para el hilo, Claude tiene que haber abierto antes ese anuncio con
     `slack_read_thread` y Slack tiene que devolver un mensaje que enlace este PR: un mensaje que imite a
     otro dentro del canal no basta. Escribir en el hilo antes que
     en Bitbucket, más de 3 mensajes, o `reply_broadcast`;
   - reacciones que no sean 👀, o ⚠️ y ✅ después del voto correspondiente;
   - los argumentos que no conoce.

   **Sin enlaces.** Slack convierte en enlace (y puede previsualizar) cualquier `algo.tld`, URL o email, así
   que un PR malicioso podría intentar sacar datos metiéndolos en un dominio. El guardián rechaza cualquier
   URL, dominio, email o esquema (`ftp:`, `mailto:`…) que no sea `https://bitbucket.org/<workspace>/<repo>/…`.
   Por eso las rutas, nombres de archivo e identificadores van entre `comillas invertidas` (Slack no enlaza
   dentro). Fuera de ellas, un `README.md` o un `this.service.run()` se bloquearían, porque `.md` y `.run`
   son TLDs reales. Claude ve el motivo del bloqueo y reescribe el mensaje.
4. **Entorno aislado.** Claude corre en un directorio vacío y privado (`~/.local/state/…/claude-cwd`), con
   un entorno mínimo: no hereda `BB_TOKEN` ni proyectos con su `.claude/settings.json`.
5. **Gasto acotado.** El límite de gasto lo pone la propia cuenta de Claude. El watcher solo limita cuántos PRs
   manda por tick, el máximo de turnos y un timeout que mata todo el grupo de procesos. Un PR que falla dos
   veces con el mismo commit deja de reintentarse.

Todos los ficheros del watcher se crean con permisos `0600` y las carpetas con `0700`.

## Notas

- El watcher **solo lee** Bitbucket con su token: lo usa para saber qué PRs hay sin pasar por Claude (es lo
  que hace que vigilar sea gratis) y para comprobar el PR antes de cada escritura. Todo lo que se publica en
  Bitbucket y Slack lo hace Claude con sus conectores, en tu nombre.
- Los timers de usuario se paran al cerrar la sesión. Para que sigan sin sesión iniciada:
  `loginctl enable-linger $USER`.
- Las units guardan el `PATH` de la terminal desde la que pulsaste `a`. Si cambias dónde está `claude`,
  vuelve a pulsar `a` dos veces (desactivar y activar).
- Pruebas: `uv run --with pytest pytest -q`. Usan un Bitbucket y un Claude falsos, así que no gastan tokens.
