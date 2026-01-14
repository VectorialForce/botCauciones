# Bot de Cauciones v2.0 - Sistema Inteligente de Notificaciones

## 🆕 Novedades en v2.0

### Sistema de Notificaciones Inteligente

El bot ahora detecta **automáticamente** cuando las tasas cambian y notifica solo a los usuarios según sus preferencias:

#### ✨ Tipos de Notificación:

1. **🔔 Cualquier Cambio**
   - Recibes una notificación cada vez que las tasas varíen
   - Perfecto para traders activos

2. **📊 Cambio Porcentual**
   - Solo te notifica cuando el cambio supere un porcentaje que elijas
   - Opciones rápidas: 0.5%, 1%, 2%, 5%
   - O configura tu propio umbral personalizado

### 🎯 Cómo Funciona

```
┌─────────────────────────────────────────┐
│  Bot verifica tasas cada 60 segundos    │
└──────────────┬──────────────────────────┘
               │
               ▼
     ¿Detecta cambios?
               │
        ┌──────┴──────┐
        │             │
       NO            SÍ
        │             │
        │             ▼
        │    ┌────────────────────┐
        │    │ Calcula % de cambio│
        │    └────────┬───────────┘
        │             │
        │             ▼
        │    Para cada usuario:
        │    ¿Cumple su umbral?
        │             │
        │      ┌──────┴──────┐
        │     SÍ            NO
        │      │              │
        │      ▼              │
        │  Notificar      Ignorar
        │      │              │
        └──────┴──────────────┘
```

## 📱 Comandos Disponibles

### `/start`
Mensaje de bienvenida con instrucciones

### `/tasas`
Consultar tasas actuales con indicador de cambios

Ejemplo de respuesta:
```
📊 TASAS DE CAUCIONES

🕐 24H: 35.50% TNA 📈 +0.25% (+0.71%)
🕑 48H: 36.20% TNA 📉 -0.10% (-0.28%)
🕒 72H: 36.80% TNA

🕒 Actualizado: 2026-01-12 14:30:45
```

### `/configurar`
Configurar tus preferencias de notificación

Muestra un menú interactivo con opciones:
- 🔔 Cualquier cambio
- 📊 Cambio > 0.5%
- 📊 Cambio > 1%
- 📊 Cambio > 2%
- 📊 Cambio > 5%
- ⚙️ Personalizado

### `/estado`
Ver tu configuración actual

Ejemplo de respuesta:
```
✅ Notificaciones activas

Tipo: 📊 Cambio > 1%
```

### `/pausar`
Pausar todas las notificaciones

### `/ayuda`
Ver lista de comandos

## 🚀 Instalación

### Requisitos
- Python 3.8+
- Cuenta PPI con acceso API
- Bot de Telegram (crear con @BotFather)

### Pasos

1. **Instalar dependencias:**
```bash
pip install "python-telegram-bot[job-queue]" python-dotenv ppi-client
```

2. **Configurar variables de entorno:**
```bash
cp .env.example .env
# Editar .env con tus credenciales
```

3. **Ejecutar:**
```bash
python caucion_bot_v2.py
```

## ⚙️ Configuración Avanzada

### Cambiar intervalo de verificación

En `caucion_bot_v2.py`, línea ~58:

```python
self.check_interval = 60  # Verificar cada 60 segundos
```

Opciones recomendadas:
- `30` = 30 segundos (más rápido, usa más recursos)
- `60` = 1 minuto (balanceado, recomendado)
- `120` = 2 minutos (más lento, menos recursos)
- `300` = 5 minutos (conservador)

⚠️ **Importante**: Intervalos muy cortos (< 30s) pueden sobrecargar la API de PPI

### Tolerancia para detección de cambios

En `caucion_bot_v2.py`, línea ~95:

```python
'changed': abs(absolute_change) > 0.001  # Tolerancia para floats
```

Ajusta este valor si quieres cambiar la sensibilidad mínima.

## 📊 Ejemplos de Uso

### Caso 1: Trader Activo

**Configuración:** 🔔 Cualquier cambio

```
Usuario: /configurar
Bot: [Muestra menú]
Usuario: [Click en "Cualquier cambio"]
Bot: ✅ Recibirás notificación cada vez que cambien

[Las tasas cambian de 35.50% a 35.55%]
Bot: 🔔 ¡Cambio en las tasas!
     24H: 35.55% 📈 +0.05% (+0.14%)
```

### Caso 2: Inversor Conservador

**Configuración:** 📊 Cambio > 2%

```
Usuario: /configurar
Bot: [Muestra menú]
Usuario: [Click en "Cambio > 2%"]
Bot: ✅ Notificaciones cuando cambio > 2%

[Las tasas cambian de 35.50% a 35.60% (+0.28%)]
Bot: [No notifica, cambio < 2%]

[Las tasas cambian de 35.50% a 36.30% (+2.25%)]
Bot: 🔔 ¡Cambio en las tasas!
     24H: 36.30% 📈 +0.80% (+2.25%)
```

### Caso 3: Umbral Personalizado

```
Usuario: /configurar
Bot: [Muestra menú]
Usuario: [Click en "Personalizado"]
Bot: Envía el porcentaje que deseas
Usuario: 1.5
Bot: ✅ Notificaciones cuando cambio > 1.5%
```

## 🔍 Monitoreo y Logs

El bot registra eventos importantes:

```
2026-01-12 14:30:00 - INFO - Bot iniciado...
2026-01-12 14:30:01 - INFO - Conectado a PPI exitosamente
2026-01-12 14:30:01 - INFO - JobQueue configurado - verificando tasas cada 60 segundos
2026-01-12 14:30:11 - INFO - Tasas iniciales guardadas
2026-01-12 14:31:11 - INFO - Cambios detectados en las tasas: {'24h': {...}}
2026-01-12 14:31:11 - INFO - Notificación enviada a 123456789
```

## 📈 Ventajas de v2.0

| Característica | v1.0 | v2.0 |
|---------------|------|------|
| **Notificaciones** | Cada 5 minutos | Solo cuando cambian |
| **Spam** | Alto | Cero |
| **Personalización** | No | Sí (umbral configurable) |
| **Detección de cambios** | No | Sí |
| **Eficiencia** | Baja | Alta |
| **Indicadores visuales** | No | Sí (📈📉 + %) |

## 🆘 Troubleshooting

### No recibo notificaciones

1. Verifica tu configuración: `/estado`
2. Asegúrate de que las tasas estén cambiando
3. Revisa que tu umbral no sea muy alto
4. Verifica los logs del bot

### Recibo demasiadas notificaciones

- Cambia a un umbral más alto: `/configurar` → Selecciona 2% o 5%

### El bot no detecta cambios

- Verifica que el `check_interval` no sea muy largo
- Revisa la conexión a PPI en los logs

## 🔐 Persistencia (Opcional)

Para guardar las suscripciones entre reinicios, agrega:

```python
import json

# Al inicio, cargar suscripciones
def load_subscriptions(self):
    try:
        with open('subscriptions.json', 'r') as f:
            data = json.load(f)
            self.subscriptions = {
                int(k): UserSubscription.from_dict(v) 
                for k, v in data.items()
            }
    except FileNotFoundError:
        pass

# Al guardar una suscripción
def save_subscriptions(self):
    with open('subscriptions.json', 'w') as f:
        data = {
            str(k): v.to_dict() 
            for k, v in self.subscriptions.items()
        }
        json.dump(data, f)
```

## 📝 Roadmap

Futuras mejoras:
- [ ] Gráficos de evolución de tasas
- [ ] Estadísticas históricas
- [ ] Alertas por Telegram channels
- [ ] Dashboard web
- [ ] Comparación con otros instrumentos

## 🤝 Contribuir

¡Mejoras y sugerencias son bienvenidas!

## 📄 Licencia

MIT License