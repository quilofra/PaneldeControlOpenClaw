# PaneldeControlOpenClaw

Panel de Control OpenClaw es una aplicación de escritorio que actúa como 
hub entre OpenClaw (u otros agentes de IA) y diferentes proveedores de IA 
(OpenAI, Anthropic, Gemini, etc.). Permite elegir manualmente el modelo 
activo, registrar las llamadas, analizar estadísticas y controlar los 
permisos de los comandos que puede ejecutar OpenClaw en su máquina.  

Desde la versión actual, el panel incluye un **bus de eventos en memoria** que 
elimina la necesidad de hacer "polling" constante de la base de datos. El 
proxy publica eventos conforme suceden (inicio de la solicitud, envío, 
recepción de tokens, finalización, errores) y la interfaz los consume en 
tiempo real para actualizar la línea de tiempo sin latencia apreciable.

El proyecto se distribuye como un repositorio de código listo para 
instalarse en cualquier máquina Linux con Python 3.10 o superior. 
Incluye una interfaz gráfica basada en Qt (PySide6), un proxy local 
autoarrancado y herramientas para integrarse con `systemd` y otros 
servicios.

## Características principales

- **Selección de proveedor y modelo activo**: puedes elegir en tiempo real 
  qué proveedor (OpenAI, Anthropic, Gemini) y modelo se utilizará para las 
  inferencias. El proxy fuerza ese modelo en todas las solicitudes.
- **Streaming de eventos y tokens**: gracias a un bus de eventos interno, el 
  panel muestra la línea de tiempo y el log en vivo a medida que llegan los 
  tokens. Los eventos se insertan en la interfaz casi al instante, con 
  tiempos relativos calculados desde el inicio de la ejecución.
- **Consola en vivo y timeline de eventos**: cada ejecución se muestra 
  con su log completo y una línea de tiempo de eventos (envío de la 
  petición, llegada del primer token, finalización del stream, errores...).
- **Historial con búsqueda y filtros**: listado de ejecuciones con filtros 
  por proveedor, estado y texto libre, incluyendo exportación a CSV y 
  cálculo de duración.
- **Permisos y sudo**: gestión de una lista blanca de comandos permitidos, 
  con posibilidad de indicar subcomandos y patrones regex para los 
  argumentos. Se pueden habilitar o deshabilitar privilegios `sudo`.
- **Estadísticas y salud**: resumen de ejecuciones, errores, tokens 
  consumidos y uso de disco; indicadores del estado del servicio 
  OpenClaw, el proxy local, la conectividad con el proveedor y la 
  conexión a Internet.
- **Gestión de claves API**: la interfaz permite introducir y validar las 
  claves API de cada proveedor. Las claves se guardan en el fichero 
  `config.json` y se aplican automáticamente. Para mayor seguridad, las 
  claves se guardan cifradas (prefijadas con `ENC:`) utilizando una clave 
  generada al vuelo y almacenada en el mismo `config.json`. El panel 
  descifra las claves al necesitarlas y nunca las escribe en texto claro 
  en los logs. Si no se dispone de la biblioteca de criptografía, las 
  claves se codifican en base64 como obfuscación básica.
- **Integración con systemd**: se incluye un script `proxy_runner.py` para 
  arrancar solo el proxy y un ejemplo de unidad `systemd` para correrlo 
  como servicio siempre activo.

## Requisitos

* Python ≥ 3.10.
* Dependencias listadas en `requirements.txt` (PySide6 y requests).
* Se recomienda ejecutar en Linux para aprovechar las herramientas de 
  `systemd`. También debería funcionar en macOS y Windows utilizando 
  solo la interfaz gráfica (sin integración con systemd).

## Instalación

1. Descarga el paquete o clona este repositorio. A partir de la
   versión 0.1.0 puedes instalarlo directamente con `pip`. Por ejemplo:

   ```bash
   # Crear un entorno virtual (recomendado)
   python3 -m venv venv
   source venv/bin/activate

   # Instalar desde el directorio actual (modo editable)
   pip install -e .
   ```

   También puedes instalarlo desde un archivo comprimido (.tar.gz/.whl)
   generado a partir de este repositorio. Consulta las instrucciones de
   empaquetado en el apartado “Distribución”.

2. Una vez instalado, dispondrás de dos comandos en tu entorno:

   - `paneldecontrolopenclaw`: ejecuta la aplicación completa (proxy + GUI).
   - `paneldecontrolopenclaw-proxy`: ejecuta solo el proxy como servicio.

   Inicia la aplicación gráfica con:

   ```bash
   paneldecontrolopenclaw
   ```

   Si `PySide6` está instalado, se abrirá la interfaz Qt. Si no, se
   utilizará el fallback con Tkinter.  El proxy se iniciará en el
   puerto definido en `config.json` (por defecto 5005). Configura
   OpenClaw para apuntar a `http://127.0.0.1:5005` y utiliza la clave
   API apropiada.

3. (Opcional) Para arrancar solo el proxy en segundo plano y
   registrarlo como servicio de sistema en Linux, copia el archivo
   `paneldecontrolopenclaw-proxy.service` que encontrarás en
   `paneldecontrolopenclaw/service/` a `/etc/systemd/system/` y
   modifícalo para indicar la ruta de instalación y el usuario que
   ejecutará el proxy. Por ejemplo:

   ```bash
   sudo cp paneldecontrolopenclaw/service/paneldecontrolopenclaw-proxy.service /etc/systemd/system/

   # Editar User y WorkingDirectory en el fichero de servicio
   sudo sed -i 's|WORKING_DIR_PLACEHOLDER|/path/a/tu/instalacion|g' /etc/systemd/system/paneldecontrolopenclaw-proxy.service
   sudo sed -i 's/USER_PLACEHOLDER/tu_usuario/g' /etc/systemd/system/paneldecontrolopenclaw-proxy.service

   sudo systemctl daemon-reload
   sudo systemctl enable paneldecontrolopenclaw-proxy.service
   sudo systemctl start paneldecontrolopenclaw-proxy.service
   ```

   De esta forma, el proxy estará siempre escuchando en segundo plano.
   Podrás lanzar la interfaz gráfica (`paneldecontrolopenclaw`) en
   cualquier momento y conectarse al proxy para ver las ejecuciones y
   estadísticas.

## Configuración

El fichero `config.json` almacena toda la configuración. Los campos más 
relevantes son:

- `provider` y `model`: definen el proveedor y el modelo activo.
- `providers`: un diccionario con las opciones de cada proveedor 
  (base URL, cabecera de la clave, prefijo y clave API).
- `allowed_commands` y `allow_sudo`: configuran los permisos de los 
  comandos que puede ejecutar OpenClaw.
- `proxy_port`: puerto en el que escuchará el proxy local.
- `theme` e `icon_variant`: personalizan el aspecto de la interfaz.

Puedes modificar `config.json` manualmente o a través de la interfaz 
gráfica (pestañas *Ajustes* y *Permisos*).

## Estructura del repositorio

```
paneldecontrolopenclaw/
│
├── backend/               # Módulos de lógica: base de datos, proxy, permisos...
├── gui/                   # Interfaces gráficas (Qt y Tkinter)
├── resources/             # Iconos e imágenes
├── service/               # Archivos para integración con systemd
├── proxy_runner.py        # Script para arrancar solo el proxy
├── main.py                # Entrada principal de la aplicación (GUI + proxy)
├── config.json            # Configuración por defecto
├── requirements.txt       # Dependencias Python
└── README.md              # Este documento
```

## Seguridad y buenas prácticas

* Las claves API se almacenan cifradas en `config.json`. Se utiliza una 
  clave de cifrado generada al iniciar la aplicación y guardada en el mismo 
  fichero. Si la librería `cryptography` no está disponible, se usa una 
  codificación base64 como obfuscación. No obstante, es recomendable
  proteger `config.json` mediante permisos de fichero para evitar accesos 
  no deseados.
* La política de permisos permite restringir los comandos que OpenClaw 
  puede ejecutar. Sé conservador y solo añade los comandos necesarios.
* El proxy registra las peticiones y respuestas en archivos de log. Se 
  redactan automáticamente los valores que parecen claves o tokens (por
  ejemplo, encabezados `Authorization`), para evitar fugas de 
  información sensible. Aun así, revisa los logs si almacenas datos 
  extremadamente delicados.

## Contribuir

Sugerencias, reportes y pull requests son bienvenidos. El objetivo de 
este proyecto es ofrecer una base extensible para conectar agentes 
locales con modelos de IA externos de forma controlada y auditada.

## Licencia

Este proyecto se distribuye bajo la licencia MIT. Consulta el archivo
`LICENSE` para más información.
