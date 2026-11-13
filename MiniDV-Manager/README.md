# MiniDV Manager

Herramienta Linux de **Obeliscum** para capturar cintas DV por FireWire, controlar la videocámara mediante AV/C y organizar un catálogo SQLite. Forma parte del proyecto de digitalización junto a DV Encoder Studio y se puede usar por separado.

## Instalar y ejecutar

Requiere Python 3.10 o posterior, Tkinter, dvgrab, dvcont y FFmpeg/ffplay. La cámara debe estar en modo reproducción/VCR y el usuario debe tener permisos para acceder a los dispositivos FireWire. El diagnóstico usa lspci.

En Debian/Ubuntu:

```bash
sudo apt update
sudo apt install python3 python3-tk dvgrab libavc1394-tools ffmpeg pciutils
python3 minidv_manager.py
```

En Arch/Manjaro instala python, tk, dvgrab, libavc1394, ffmpeg y pciutils con el gestor de paquetes de tu distribución. El nombre y disponibilidad de los paquetes de captura pueden variar.

Para crear el lanzador del usuario:

```bash
python3 install.py
```

## Configurar las rutas

En **Captura** elige la carpeta temporal local. En **Destino de archivo** elige una carpeta local, de disco externo o de red ya montada. Las capturas se transfieren a subcarpetas CODIGO_TITULO. En **Conversión FFmpeg** elige origen y salida de los MP4.

Pulsa **Guardar rutas** o inicia una operación para recordar las rutas. Se guardan también al cerrar. Usa **Catálogo SQLite → Elegir…** para abrir un catálogo existente o crear uno en otra ubicación. El catálogo previo permanece en su archivo anterior.

Las preferencias se guardan en `~/.config/minidv-manager/settings.json`, respetando XDG_CONFIG_HOME. El catálogo nuevo usa XDG_DATA_HOME o `~/.local/share/minidv-manager/catalog.db`. Si existe `~/minidv_catalog.db`, se usa como catálogo inicial para conservar las instalaciones anteriores.

## Capturar y elegir cómo se divide la cinta

1. Introduce código y título de cinta.
2. Elige la carpeta temporal y el modo de división.
3. Activa la vista previa si la necesitas y pulsa Iniciar.
4. Usa Detener antes de desconectar la cámara.
5. Al terminar puedes mover las capturas a la carpeta de archivo elegida.

Los modos de división son:

- **Por tomas originales** conserva el comportamiento anterior. dvgrab usa `--autosplit`, que separa por marcas de nueva grabación o saltos de código de tiempo. Puede crear archivos de un segundo o incluso de pocos fotogramas.
- **Continuo (cinta completa)** desactiva autosplit. Usa este modo para evitar la fragmentación por tomas durante una captura ininterrumpida.
- **Pausas de 60 s en fecha/hora** usa `--autosplit=60` para separar según huecos de al menos un minuto en la fecha/hora de grabación, en lugar de usar cada marca de nueva toma.

El modo elegido se recuerda. Se conserva **Por tomas originales** como valor inicial para mantener el comportamiento de las instalaciones anteriores. Todos los modos usan `--size 0 --frames 0` para desactivar los límites de tamaño y fotogramas del programa. El sistema de archivos y el espacio libre siguen limitando la captura.

`--timestamp` pone la fecha y hora originales en el nombre, cuando están disponibles. No es el causante de los cortes. En captura continua, un único archivo puede contener fechas distintas. El creador de recuerdos actual no subdivide un DV largo por sus fechas internas.

Los clips diminutos pueden proceder tanto de tomas breves originales como de metadatos discontinuos o errores de lectura. Para separar esas causas revisa los mensajes de dvgrab y la reproducción de la cinta. [Manual original de dvgrab](https://github.com/ddennedy/dvgrab/blob/master/dvgrab.1).

## Catálogo, transferencias y conversión

La base SQLite guarda fichas, fechas, etiquetas, notas y estados. El borrado de una ficha puede ofrecer borrar también archivos asociados. Revisa la confirmación porque se trata de un borrado permanente.

La transferencia conserva el original local si falla la copia y rechaza sobrescribir un archivo de igual nombre ya existente. Las transferencias grandes a red dependen del montaje y los permisos. Comprueba que la ruta de red esté montada antes de mover archivos.

La conversión integrada produce MP4 H.264 con audio AAC y mantiene la proporción original. Opera sobre los DV del primer nivel de la carpeta elegida y conserva los MP4 ya existentes. Para colas más completas, HEVC, Intel VA-API y creación de recuerdos usa DV Encoder Studio.

La captura actual requiere supervisión de espacio y señal. El bloqueo de suspensión de DV Encoder Studio protege su cola de codificación. MiniDV Manager no implementa ese bloqueo para la captura.

## Empaquetar en Linux

Ejecutar Python directamente es suficiente. Para distribuir un ejecutable:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-build.txt
python -m PyInstaller --noconfirm --clean --onedir --windowed \
  --name minidv-manager --add-data "video-tape.png:." minidv_manager.py
```

En Debian/Ubuntu puede ser necesario instalar python3-venv. Distribuye toda la carpeta `dist/minidv-manager/` junto con README y LICENSE. dvgrab, dvcont, FFmpeg y los controladores FireWire se instalan aparte. Construye en una distribución Linux igual o anterior a la de destino.

## Validación y autoría

Se verificaron las rutas persistentes, los catálogos, la selección de división y el lanzamiento de captura con y sin vista previa usando procesos simulados. La comprobación con una cámara real queda pendiente.

Copyright © 2026 **Obeliscum**. Licencia MIT en [LICENSE](LICENSE). Conserva la autoría y el texto de licencia al redistribuir.
