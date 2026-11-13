# DV Encoder Studio

Aplicación de escritorio para Linux de **Obeliscum**, con dos herramientas integradas.

- Codificador MiniDV a H.265 con Intel VA-API o CPU libx265.
- Creador local de recuerdos con títulos editables, capítulos, transiciones y música en bucle.

![Icono](dv-encoder-studio.png)

## Requisitos

Python 3.10 o posterior, Tkinter, FFmpeg y FFprobe. El bloqueo de suspensión requiere Linux con systemd-logind, acceso al bus del sistema y dbus-python. ExifTool es opcional y permite recuperar fechas de grabación de las cintas. Para títulos se necesita un FFmpeg con drawtext y fuentes Sans/Serif.

En Debian o Ubuntu:

```bash
sudo apt update
sudo apt install python3 python3-tk python3-dbus ffmpeg libimage-exiftool-perl fonts-dejavu-core
```

En Arch o Manjaro:

```bash
sudo pacman -S python tk python-dbus ffmpeg perl-image-exiftool ttf-dejavu
```

Intel necesita una GPU compatible con codificación HEVC y el controlador VA-API adecuado para su generación. El usuario debe tener permiso para acceder a su nodo render. CPU Fast sirve cuando falta ese hardware.

## Ejecutar e instalar el lanzador

Descarga o clona el repositorio y abre una terminal dentro de la carpeta del programa.

```bash
python3 dv_encoder_studio.py
```

Para añadirlo al menú de aplicaciones del usuario actual:

```bash
python3 install.py
```

El instalador genera las rutas del lanzador a partir de la ubicación real del programa. Conserva esa carpeta después de instalarlo. Si la mueves, vuelve a ejecutar el instalador.

## Carpetas y preferencias

En la ventana principal selecciona la carpeta de origen y, opcionalmente, el destino de los MP4. Pulsa **Refrescar lista** tras modificar las rutas.

- Con destino vacío, cada MP4 se guarda en `codificados/` junto a su cinta.
- Con destino elegido, se reproduce debajo de él la estructura de carpetas relativa al origen. Dos cintas con clips de igual nombre conservan carpetas diferentes.
- La búsqueda excluye `codificados/` y el destino seleccionado.
- Cambiar el destino cambia qué MP4 revisa la cola. Los archivos anteriores siguen en su ubicación previa.
- El motor y la calidad se guardan al iniciar una codificación.
- Las preferencias se guardan en `$XDG_CONFIG_HOME/dv-encoder-studio/settings.json` o, por defecto, `~/.config/dv-encoder-studio/settings.json`.

En **Crear recuerdo** puedes añadir varias carpetas, elegir tu música y una carpeta de salida. La app recuerda esas rutas. El diálogo final permite elegir el nombre y la ubicación de cada película.

## Codificar una cinta o una cola

**Codificar selección** convierte el archivo seleccionado y pide confirmación si existe un MP4. **Codificar todo en cola** convierte pendientes y revisa los MP4 existentes. Salta los que cumplen las comprobaciones de duración, proporción, HEVC y número de pistas de audio. Si todos tienen MP4, pregunta si deseas recodificarlos todos. Responder No cancela la operación.

Intel usa QP. CPU usa CRF. Un valor menor conserva normalmente más detalle y genera archivos mayores. El mismo número puede dar resultados diferentes según el motor. CPU Slow dedica más tiempo a comprimir que CPU Fast. Las ayudas flotantes explican estos controles.

La salida conserva la proporción de imagen y desentrelaza DV en doble frecuencia. Antes de sustituir un MP4 se termina y comprueba un archivo temporal en su mismo directorio. Detener o cerrar cancela la cola y espera a FFmpeg.

Durante la cola se mantiene un único bloqueo de suspensión e hibernación. Si el sistema no concede el bloqueo, el procesamiento se detiene y muestra el motivo. El bloqueo se libera al finalizar o cancelar. Un apagado forzado o una orden administrativa que ignore los inhibidores puede interrumpirlo.

## Crear un recuerdo

1. Pulsa **Crear recuerdo…**.
2. Añade una o varias carpetas y pulsa **Analizar carpetas**.
3. Revisa los capítulos. Haz doble clic o pulsa **Editar título…** para cambiar su texto.
4. Escribe el título general, la duración máxima, el estilo de títulos y las transiciones.
5. Selecciona la carpeta de música y el destino.
6. Pulsa **Generar vídeo…** y elige el nombre del MP4.

Las fechas proceden de los metadatos DV, de nombres de archivo o carpeta y, como último recurso, de los metadatos del contenedor. Revisa las fechas propuestas porque la fecha de creación del MP4 puede corresponder a su conversión. Los lugares proceden de etiquetas existentes o nombres de carpeta.

El montaje selecciona ventanas repartidas a lo largo de cada capítulo en orden. Usa reglas y filtros locales. Esta versión carece de un modelo que reconozca emociones, personas o monumentos. Una duración máxima no garantiza llenar ese tiempo si falta material utilizable.

La exportación es MP4 H.264 de 1024×576 a 25 fps con audio AAC. Conserva las proporciones mediante bandas cuando son necesarias. Los fundidos son a negro, blanco o alternos entre capítulos. Los títulos ofrecen tres diseños o alternancia automática.

Las pistas se usan por orden natural de nombre. La selección se repite hasta cubrir el vídeo, baja de volumen según el sonido original y termina con un fundido. Este control responde a energía en el rango de voz, por lo que también reacciona a otros sonidos.

Se excluyen los vídeos de menos de tres segundos y los que carecen de suficientes píxeles visibles en los fotogramas muestreados. La detección es aproximada y puede descartar escenas nocturnas útiles.

**Revisar descartados…** permite reproducir y seleccionar archivos antes de eliminarlos definitivamente. Se ofrecen el DV y su pareja MP4 identificable por nombre y ubicación, incluida la salida personalizada actualmente configurada. Se omiten asociaciones ambiguas. Revisa la selección porque el borrado es permanente.

## Muchos clips de pocos segundos al digitalizar

La división se realiza en el programa de captura. El análisis de recuerdos conserva los DV originales.

Con `dvgrab`, `-autosplit` abre archivos al detectar una marca de nueva grabación o discontinuidades del código de tiempo. Las marcas suelen corresponder a Grabar/Parar en la cámara. Un código de tiempo dañado también puede provocar fragmentación. No equivale simplemente a separar por día.

`-timestamp` incorpora fecha y hora al nombre. `-frames` puede imponer un máximo de fotogramas por archivo y `-size` un máximo de tamaño. Examina el comando real y los registros para identificar la causa.

Ejemplo de captura continua, con cámara FireWire y dvgrab configurados:

```bash
mkdir -p "$HOME/Videos/Mi-cinta"
cd "$HOME/Videos/Mi-cinta"
dvgrab -format raw -size 0 -timestamp cinta-
```

Omite `-autosplit` y límites de fotogramas. Ctrl+C detiene la captura. Para separar nuevas grabaciones se puede añadir `-autosplit`. Para dividir según huecos en la fecha/hora de grabación, dvgrab admite `-autosplit=segundos`, según la versión.

Una captura continua conserva los metadatos originales entregados por la cámara, aunque el nombre represente solo su inicio. El creador de recuerdos actual no divide un DV largo por cambios internos de fecha.

Consulta el [código y manual de dvgrab](https://github.com/ddennedy/dvgrab). Los cortes por pérdida de señal o fallos FireWire requieren revisar los registros del dispositivo.

## Empaquetar como ejecutable Linux

El código Python se ejecuta directamente. Para crear una distribución con PyInstaller, construye en Linux en una distribución igual o anterior a las de destino.

Instala las dependencias del sistema anteriores. En Debian/Ubuntu instala también `python3-venv`. Crea un entorno que pueda usar dbus-python:

```bash
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
python -m pip install -r requirements-build.txt
python -m PyInstaller --noconfirm --clean --onedir --windowed \
  --name dv-encoder-studio \
  --add-data "dv-encoder-studio.png:." \
  --hidden-import=dbus \
  dv_encoder_studio.py
```

El resultado es `dist/dv-encoder-studio/`. Distribuye la carpeta completa con README y LICENSE. FFmpeg, FFprobe, ExifTool y systemd-logind siguen siendo dependencias del sistema. El binario es para Linux y su arquitectura de construcción.

La plantilla `.desktop` del repositorio está dirigida a paquetes que instalen ejecutable e icono en el sistema. Desde código fuente usa `install.py`.

## Autoría y licencia

Copyright © 2026 **Obeliscum**. Licencia MIT, incluida en [LICENSE](LICENSE).

Se permite usar, modificar y redistribuir conservando el aviso de copyright y la licencia. Los vídeos y música de cada usuario tienen sus propias condiciones y no forman parte del código distribuido.

## Validación de esta versión

Se verificaron las rutas persistentes y salidas separadas para dos cintas con nombres de archivo iguales. Se construyó y abrió el ejecutable con PyInstaller 6.22.3 en Linux x86_64. La portabilidad a otras distribuciones y el hardware Intel dependen del equipo de destino.
