# steam-wsdl

Descargador de mods del **Steam Workshop** para la línea de comandos, con descargas concurrentes, reanudación de descargas interrumpidas e interfaz enriquecida en la terminal.

Le pasas enlaces (de un elemento, de varios o de un archivo `.txt`) y el programa descarga todo a una carpeta, en paralelo, mostrando el progreso de cada elemento.

## Índice

- [Para qué sirve](#para-qué-sirve)
- [Cómo funciona](#cómo-funciona)
- [Instalación](#instalación)
- [Uso](#uso)
- [Opciones](#opciones)
- [Formato del archivo de enlaces](#formato-del-archivo-de-enlaces)
- [Estructura de la carpeta de salida](#estructura-de-la-carpeta-de-salida)
- [Códigos de salida](#códigos-de-salida)
- [Solución de problemas](#solución-de-problemas)
- [Desarrollo](#desarrollo)

## Para qué sirve

- Descargar varios mods a la vez a partir de una lista de enlaces.
- Descargar un único elemento mediante su enlace (o su ID).
- Expandir las **colecciones** del Workshop y descargar todos los elementos que contienen.
- Reanudar descargas interrumpidas y reintentar automáticamente ante fallos de red.
- Generar una lista con los enlaces que fallaron, para volver a intentarlo después.

## Cómo funciona

El programa consulta la API pública de Steam (sin clave) para obtener el título, el juego y el tamaño de cada elemento, y elige uno de dos métodos:

| Método | Cuándo se usa | Observaciones |
|---|---|---|
| **Directo** | El elemento expone una URL de descarga directa (habitual en juegos más antiguos, como Garry's Mod). | Usa solo HTTP, con varias descargas en paralelo (`-w`), reanudación mediante `Range` y reintentos con espera creciente. |
| **`steamcmd`** | El elemento no tiene URL directa (habitual en juegos modernos). | Requiere tener `steamcmd` instalado. Cada sesión inicia sesión **una sola vez** y descarga varios elementos en secuencia; varias sesiones se ejecutan en paralelo. |

Si la descarga directa falla y `steamcmd` está disponible, el elemento pasa automáticamente a `steamcmd`.

> **Atención:** algunos juegos solo permiten descargar elementos del Workshop con una cuenta que posea el juego. En esos casos el inicio de sesión anónimo de `steamcmd` falla, y el elemento aparece como fallido con un mensaje que explica el motivo (consulta `--steam-user`).

## Instalación

### Requisitos

- Python 3.9 o superior
- [uv](https://docs.astral.sh/uv/) (recomendado)
- `steamcmd` (opcional, pero necesario para elementos sin descarga directa)

### Método recomendado: `uv tool install` para todo el sistema

En la carpeta del proyecto (la que contiene `pyproject.toml`), ejecuta:

```bash
sudo UV_TOOL_DIR=/opt/steam-wsdl UV_TOOL_BIN_DIR=/usr/local/bin "$(which uv)" tool install .
```

Esto instala el programa en un entorno aislado en `/opt/steam-wsdl` y crea el comando `steam-wsdl` en `/usr/local/bin`, que ya está en el PATH. Después, desde cualquier carpeta:

```bash
steam-wsdl --help
```

Puntos importantes sobre este comando:

- `"$(which uv)"` se resuelve **antes** de `sudo`, así que funciona incluso si `uv` no está en el PATH de root.
- `uv tool install .` **copia** el código al entorno. Puedes mover o borrar la carpeta del proyecto sin afectar al comando instalado.
- El entorno se crea como root y usa el Python que `uv` elija. Si `uv` descarga un Python en la carpeta de root, es posible que otros usuarios no puedan ejecutar el comando. Para usar el Python del sistema, añade `--python /usr/bin/python3` (debe cumplir el `requires-python` del `pyproject.toml`):

  ```bash
  sudo UV_TOOL_DIR=/opt/steam-wsdl UV_TOOL_BIN_DIR=/usr/local/bin "$(which uv)" tool install --python /usr/bin/python3 .
  ```

**Actualizar** (después de modificar el código):

```bash
sudo UV_TOOL_DIR=/opt/steam-wsdl UV_TOOL_BIN_DIR=/usr/local/bin "$(which uv)" tool install --force .
```

**Desinstalar:**

```bash
sudo UV_TOOL_DIR=/opt/steam-wsdl UV_TOOL_BIN_DIR=/usr/local/bin "$(which uv)" tool uninstall steam-wsdl
```

### Instalación solo para tu usuario

Sin `sudo`, el comando se instala en `~/.local/bin`:

```bash
uv tool install .
uv tool update-shell   # si no se encuentra el comando, ejecútalo y abre una nueva terminal
```

### Sin instalar nada

```bash
uv run steam-wsdl -u "https://steamcommunity.com/sharedfiles/filedetails/?id=123456789"
```

### Instalar `steamcmd`

Solo es necesario para elementos sin descarga directa.

- **Debian/Ubuntu:** `sudo apt install steamcmd` (puede requerir habilitar el repositorio `multiverse` y la arquitectura `i386`).
- **Arch Linux:** paquete `steamcmd` en el AUR.
- **Otros sistemas / Windows / macOS:** sigue las instrucciones de Valve en <https://developer.valvesoftware.com/wiki/SteamCMD>.

El programa busca `steamcmd`, `steamcmd.sh` o `steamcmd.exe` en el PATH. Si está en otro lugar, usa `--steamcmd RUTA`.

## Uso

Un único elemento:

```bash
steam-wsdl -u "https://steamcommunity.com/sharedfiles/filedetails/?id=123456789" -o ./mods
```

El argumento `-u` también acepta solo el ID numérico y se puede repetir:

```bash
steam-wsdl -u 123456789 -u 987654321 -o ./mods
```

Una lista en un archivo (un enlace por línea):

```bash
steam-wsdl -f mods.txt -o ./mods -w 8
```

Combinando ambas formas y guardando los fallos para reintentarlos:

```bash
steam-wsdl -f mods.txt -u 987654321 --failed-file fallos.txt
steam-wsdl -f fallos.txt            # reintenta solo los que fallaron
```

Los elementos ya descargados se **omiten**, así que ejecutar el mismo comando de nuevo es seguro. Usa `--overwrite` para volver a descargarlos.

Interrumpir con **Ctrl+C** cierra el programa con calma. Los archivos parciales (`.part`) se conservan y se reanudan en la siguiente ejecución.

## Opciones

### Entrada (se requiere al menos una)

| Opción | Descripción |
|---|---|
| `-u`, `--url ENLACE` | Enlace (o ID) de un elemento del Workshop. Se puede repetir. |
| `-f`, `--file ARCHIVO.txt` | Archivo de texto con un enlace por línea. |

### General

| Opción | Por defecto | Descripción |
|---|---|---|
| `-o`, `--output CARPETA` | `workshop_downloads` | Carpeta de salida (se crea si no existe). |
| `-w`, `--workers N` | `4` | Descargas simultáneas, de 1 a 32. También define el número de sesiones de `steamcmd`. |
| `-r`, `--retries N` | `3` | Intentos adicionales ante fallos de red, de 0 a 20. |
| `-t`, `--timeout SEG` | `30` | Tiempo límite de red en segundos, de 5 a 600. |
| `--overwrite` | desactivado | Vuelve a descargar los elementos que ya existen. |
| `--failed-file ARCHIVO.txt` | — | Escribe en este archivo los enlaces que fallaron. |
| `-V`, `--version` | — | Muestra la versión. |
| `-h`, `--help` | — | Muestra la ayuda. |

### `steamcmd`

| Opción | Por defecto | Descripción |
|---|---|---|
| `--steamcmd RUTA` | busca en el PATH | Ruta del ejecutable de `steamcmd`. |
| `--no-steamcmd` | desactivado | Nunca usa `steamcmd`. Los elementos sin descarga directa fallan. |
| `--force-steamcmd` | desactivado | Usa `steamcmd` para todos los elementos. |
| `--steam-user USUARIO` | `anonymous` | Cuenta de Steam con inicio de sesión **ya guardado** en `steamcmd`. Fuerza 1 sesión, porque la misma cuenta no puede iniciar sesión en paralelo. |
| `--steamcmd-sessions N` | igual que `-w` | Sesiones simultáneas de `steamcmd` (1 a 16). Cada sesión inicia sesión una vez y descarga varios elementos en secuencia. |
| `--steamcmd-timeout SEG` | `900` | Cierra una sesión tras SEG segundos **sin actividad** (30 a 7200). |

Consejo de rendimiento: cada sesión de `steamcmd` descarga **un elemento a la vez**, así que el paralelismo es igual al número de sesiones. Si aparecen fallos extraños con muchas sesiones, redúcelo con `--steamcmd-sessions 2` o `3`.

## Formato del archivo de enlaces

Una entrada por línea. Acepta enlaces completos o IDs numéricos. Las líneas vacías y las que empiezan por `#` o `;` se ignoran, y solo se usa la primera palabra de cada línea (el resto puede servir de anotación).

```text
# Mods del servidor
https://steamcommunity.com/sharedfiles/filedetails/?id=123456789
https://steamcommunity.com/workshop/filedetails/?id=987654321
555444333        # solo el ID también funciona
; colección completa
https://steamcommunity.com/sharedfiles/filedetails/?id=111222333
```

Los enlaces no válidos no interrumpen la ejecución: el programa avisa del número de línea y continúa con los demás. El archivo debe estar en UTF-8.

## Estructura de la carpeta de salida

Los elementos se organizan por el ID del juego (App ID):

```text
workshop_downloads/
└── 108600/                                  # App ID del juego
    ├── Mi Mod [123456789].gma               # descarga directa: un archivo
    └── Otro Mod [987654321]/                # vía steamcmd: una carpeta
        └── ...
```

Los caracteres no válidos en nombres de archivo se sustituyen por `_`.

## Códigos de salida

| Código | Significado |
|---|---|
| `0` | Todo correcto. |
| `1` | Al menos un elemento falló (o hubo un error al consultar el Workshop). |
| `2` | Error de uso: argumentos no válidos, archivo de enlaces ausente o ilegible, carpeta de salida sin permiso de escritura. |
| `130` | Interrumpido con Ctrl+C. |

Esto permite usar el programa en scripts:

```bash
steam-wsdl -f mods.txt -o ./mods || echo "algunos elementos fallaron"
```

## Solución de problemas

| Síntoma | Causa probable y solución |
|---|---|
| `sem URL de download direto ... instale o steamcmd` (sin URL de descarga directa ... instala steamcmd) | El elemento no tiene descarga directa y no se encontró `steamcmd`. Instálalo o usa `--steamcmd RUTA`. |
| `a Steam exige uma conta que possua o jogo` (Steam exige una cuenta que posea el juego) | El juego no permite descargas anónimas. Inicia sesión una vez en `steamcmd` con tu cuenta y usa `--steam-user TU_USUARIO`. |
| `item não encontrado, removido ou privado` (elemento no encontrado, eliminado o privado) | El elemento fue borrado o no es público. |
| `steamcmd ficou N s sem atividade` (steamcmd estuvo N s sin actividad) | La sesión se bloqueó o se cayó la conexión. Ejecuta de nuevo (los elementos ya descargados se omiten) o aumenta `--steamcmd-timeout`. |
| Fallos aleatorios con muchas sesiones | Reduce `--steamcmd-sessions`. |
| `steam-wsdl: command not found` | Comprueba que `/usr/local/bin` (o `~/.local/bin`) esté en el PATH. Con `uv tool install` sin `sudo`, ejecuta `uv tool update-shell`. |
| `Dependência ausente: rich` (falta la dependencia: rich) | El entorno no tiene sus dependencias. Reinstala con `uv tool install --force .`. |
| `não foi possível usar a pasta de saída` (no se pudo usar la carpeta de salida) | Sin permiso de escritura en la carpeta indicada en `-o`. |

## Desarrollo

```bash
git clone <URL-DEL-REPOSITORIO>
cd steam-wsdl
uv sync                          # crea el .venv con las dependencias
uv run steam-wsdl --help         # se ejecuta directamente desde el código fuente
uv tool install -e .             # opcional: un comando que refleja tus ediciones al instante
```

Dependencias de ejecución: [`requests`](https://requests.readthedocs.io/) y [`rich`](https://rich.readthedocs.io/).

## Aviso

Descarga solo contenido que tengas derecho a usar y respeta las condiciones de uso de Steam y las licencias de cada mod.