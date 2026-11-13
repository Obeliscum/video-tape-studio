# Video Tape Studio

Proyecto de **Obeliscum** para digitalizar, conservar y transformar cintas de vídeo antiguas en recuerdos familiares. Incluye dos aplicaciones Linux que pueden utilizarse juntas o por separado.

| Aplicación | Función | Guía |
| --- | --- | --- |
| MiniDV Manager | Captura por FireWire, control de videocámara, catálogo SQLite y organización de cintas | [Instalar, configurar y usar](MiniDV-Manager/README.md) |
| DV Encoder Studio | Compresión HEVC por Intel VA-API o CPU y creación local de recuerdos con títulos, transiciones y música | [Instalar, configurar y usar](DV-Encoder-Studio/README.md) |

## Descarga y puesta en marcha

Requiere Linux y Python 3.10 o posterior. Clona el proyecto o usa **Code → Download ZIP** en GitHub.

```bash
git clone https://github.com/Obeliscum/dv-encoder-studio.git
cd dv-encoder-studio
```

En Debian/Ubuntu instala las dependencias:

```bash
sudo apt update
sudo apt install python3 python3-tk python3-dbus ffmpeg libimage-exiftool-perl \
  dvgrab libavc1394-tools pciutils fonts-dejavu-core
```

Ejecuta cada aplicación:

```bash
python3 MiniDV-Manager/minidv_manager.py
python3 DV-Encoder-Studio/dv_encoder_studio.py
```

Cada comando abre una app independiente. Para añadir accesos al menú del usuario:

```bash
python3 MiniDV-Manager/install.py
python3 DV-Encoder-Studio/install.py
```

Mantén las carpetas de los programas en la misma ubicación tras instalar los accesos. Las guías de cada app incluyen dependencias de otras distribuciones e instrucciones de empaquetado con PyInstaller.

## Flujo de trabajo

1. En MiniDV Manager elige una carpeta temporal local y captura la cinta en DV.
2. Transfiere las capturas a tu carpeta de archivo, disco externo o NFS ya montado.
3. En DV Encoder Studio elige esa carpeta como origen y configura el destino de los MP4.
4. Convierte los pendientes con CPU o Intel, según tu hardware.
5. Abre **Crear recuerdo**, añade carpetas de un viaje o evento, revisa los títulos y selecciona tu música.
6. Genera un montaje con la duración máxima, diseños y fundidos que prefieras.

## Rutas elegidas por cada usuario

Las aplicaciones permiten seleccionar sus carpetas de origen y destino sin editar el código. Recuerdan los ajustes en carpetas de configuración del usuario. La elección de rutas en una app es independiente de la otra, por lo que puedes instalarlas en ordenadores diferentes y usar un archivo compartido por red.

MiniDV Manager también permite elegir el archivo del catálogo SQLite. DV Encoder Studio conserva la jerarquía de cintas si eliges una salida centralizada. Cada guía explica las opciones y sus valores iniciales.

## Evitar cientos de clips diminutos

La versión anterior de MiniDV Manager pasaba siempre `--autosplit` a dvgrab. Esto separa por marcas de nueva toma y discontinuidades del código de tiempo, incluso en el mismo día. Puede reflejar pulsaciones originales de Grabar/Parar o metadatos dañados.

Ahora puedes elegir **Dividir captura → Continuo (cinta completa)**. Para conservar la separación original selecciona **Por tomas originales**. También puedes agrupar usando **Pausas de 60 s en fecha/hora**. El modo se guarda para futuras sesiones. Se mantiene el modo por tomas como valor inicial para conservar el comportamiento anterior.

Una captura continua conserva en su flujo los metadatos entregados por la cámara. La versión actual del creador de recuerdos no separa automáticamente un DV largo por sus cambios internos de fecha. Si necesitas capítulos por fecha, considera capturas por tomas o por pausas y revisa los títulos propuestos.

El creador de recuerdos excluye los clips de menos de tres segundos y los que parecen negros o muy oscuros. Su análisis usa fotogramas muestreados y puede equivocarse con escenas nocturnas. **Revisar descartados…** permite comprobarlos antes de decidir un borrado permanente.

## Alcance y pruebas

Se verificaron las rutas persistentes de ambas apps, salidas diferenciadas por cinta y asociaciones entre DV y MP4. Se probaron los modos de captura con procesos simulados, tanto con vista previa como sin ella. Se construyeron ejecutables Linux mediante PyInstaller. Las conversiones y montajes se comprobaron con clips sintéticos.

La compatibilidad con una cámara y capturadora reales, los permisos FireWire, el controlador Intel y los montajes de red dependen de cada instalación. El montaje automático selecciona fragmentos por reglas y datos disponibles. Los lugares proceden de etiquetas o nombres de carpeta y los títulos se pueden editar.

## Autoría y licencia

Copyright © 2026 **Obeliscum**. Código e iconos propios bajo licencia [MIT](LICENSE). Se permite usar, modificar y redistribuir conservando el aviso de copyright y el texto de licencia.

FFmpeg, dvgrab, dvcont, Python y las demás dependencias tienen sus propias licencias. Las grabaciones y pistas musicales pertenecen a sus titulares y se guardan en las carpetas que elija cada usuario.
