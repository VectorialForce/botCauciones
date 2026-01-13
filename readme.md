# Bot de Tasas de Cauciones para Telegram

Bot que obtiene y publica en tiempo real las tasas de cauciones desde PPI y las envía a través de Telegram.

## 🚀 Características

- ✅ Consulta de tasas en tiempo real (24h, 48h, 72h)
- ✅ Actualizaciones automáticas cada 5 minutos para suscriptores
- ✅ Comandos simples y fáciles de usar
- ✅ Sistema de suscripción/pausa de notificaciones

## 📋 Requisitos

- Python 3.8+
- Cuenta en PPI con acceso API
- Bot de Telegram (crear con @BotFather)

## 🔧 Instalación

1. **Clonar o descargar el código**

2. **Instalar dependencias:**
```bash
pip install python-telegram-bot python-dotenv ppi-client
```

3. **Crear bot de Telegram:**
   - Habla con [@BotFather](https://t.me/botfather) en Telegram
   - Envía el comando `/newbot`
   - Sigue las instrucciones
   - Guarda el token que te proporciona

4. **Configurar variables de entorno:**
   - Copia `.env.example` a `.env`
   - Completa con tus credenciales:
     ```
     PPI_PUBLIC_KEY=tu_public_key
     PPI_SECRET_KEY=tu_secret_key
     TELEGRAM_BOT_TOKEN=tu_token_de_telegram
     ```

## 🎮 Uso

### Iniciar el bot:
```bash
python caucion_bot.py
```

### Comandos disponibles en Telegram:

- `/start` - Iniciar el bot y ver comandos
- `/tasas` - Ver tasas actuales de cauciones
- `/suscribir` - Activar actualizaciones automáticas cada 5 minutos
- `/pausar` - Pausar actualizaciones automáticas
- `/ayuda` - Ver ayuda de comandos

## 📊 Ejemplo de salida

```
📊 TASAS DE CAUCIONES

🕐 24 horas: 35.50% TNA
🕑 48 horas: 36.20% TNA
🕒 72 horas: 36.80% TNA

🕒 Actualizado: 2026-01-12 14:30:45
```

## ⚙️ Personalización

### Cambiar intervalo de actualizaciones

En el archivo `caucion_bot.py`, modifica esta línea:

```python
application.job_queue.run_repeating(
    self.send_rates_to_subscribers,
    interval=300,  # Cambiar este valor (en segundos)
    first=10
)
```

Ejemplos:
- `60` = 1 minuto
- `300` = 5 minutos (por defecto)
- `600` = 10 minutos
- `3600` = 1 hora

### Cambiar formato del mensaje

Modifica el método `format_rates_message()` en la clase `CaucionBot`.

## 🛠️ Solución de problemas

### Error: "TELEGRAM_BOT_TOKEN no configurado"
- Asegúrate de tener el archivo `.env` con el token

### Error de conexión a PPI
- Verifica tus credenciales PPI en `.env`
- Comprueba que tu cuenta tenga acceso API habilitado

### El bot no responde
- Verifica que el bot esté corriendo
- Busca tu bot en Telegram por el username que le asignaste
- Presiona "Start" para iniciar la conversación

## 📝 Estructura del código

```
caucion_bot.py
├── PPIConfig          # Configuración de PPI
├── CaucionBot         # Clase principal del bot
│   ├── connect_ppi()              # Conectar a PPI
│   ├── get_caucion_rates()        # Obtener tasas
│   ├── format_rates_message()     # Formatear mensaje
│   ├── start_command()            # Handler /start
│   ├── tasas_command()            # Handler /tasas
│   ├── suscribir_command()        # Handler /suscribir
│   ├── pausar_command()           # Handler /pausar
│   └── send_rates_to_subscribers() # Envío periódico
└── main()             # Punto de entrada
```

## 🔒 Seguridad

- **Nunca** compartas tu archivo `.env`
- **Nunca** subas tus tokens a repositorios públicos
- Agrega `.env` a tu `.gitignore`

## 📄 Licencia

Este código es de ejemplo educativo. Úsalo bajo tu propia responsabilidad.

## 🤝 Contribuciones

¡Las mejoras son bienvenidas! Algunas ideas:

- Agregar más tipos de cauciones
- Gráficos de evolución de tasas
- Alertas cuando las tasas suben/bajan cierto porcentaje
- Múltiples intervalos de actualización personalizables
- Base de datos para historial de tasas