from enum import Enum
from ppi_client.ppi import PPI
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from os import getenv
from dataclasses import dataclass
import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from pathlib import Path
from typing import Dict
import sqlite3

load_dotenv()

# Timezone de Argentina
ARGENTINA_TZ = ZoneInfo("America/Buenos_Aires")

# Horario del mercado de cauciones (hora Argentina)
MARKET_OPEN_HOUR = 10
MARKET_OPEN_MINUTE = 30
MARKET_CLOSE_HOUR = 17
MARKET_CLOSE_MINUTE = 00


# Configurar logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


class Environment(Enum):
    PRODUCTION = "production"
    SANDBOX = "sandbox"


@dataclass(frozen=True)
class PPIConfig:
    public_key: str
    private_key: str
    sandbox: bool

    @staticmethod
    def from_environment(env: Environment) -> "PPIConfig":
        if env == Environment.PRODUCTION:
            return PPIConfig(
                public_key=getenv("PPI_PUBLIC_KEY"),
                private_key=getenv("PPI_SECRET_KEY"),
                sandbox=False
            )

        if env == Environment.SANDBOX:
            return PPIConfig(
                public_key=getenv("PPI_SANDBOX_PUBLIC_KEY"),
                private_key=getenv("PPI_SANDBOX_SECRET_KEY"),
                sandbox=True
            )

        raise ValueError(f"Unsupported environment: {env}")


class SubscriptionType(Enum):
    NONE = "none"  # Sin suscripción
    ANY_CHANGE = "any_change"  # Cualquier cambio
    PERCENTAGE = "percentage"  # Cambio porcentual


@dataclass
class UserSubscription:
    chat_id: int
    subscription_type: SubscriptionType
    threshold_percentage: float = 0.0  # % de cambio para notificar

    def to_dict(self):
        return {
            'chat_id': self.chat_id,
            'subscription_type': self.subscription_type.value,
            'threshold_percentage': self.threshold_percentage
        }

    @staticmethod
    def from_dict(data):
        return UserSubscription(
            chat_id=data['chat_id'],
            subscription_type=SubscriptionType(data['subscription_type']),
            threshold_percentage=data.get('threshold_percentage', 0.0)
        )


class SQLitePersistence:
    """Maneja persistencia de datos en SQLite"""

    def __init__(self, db_path: str = "data/bot.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(exist_ok=True)

        # Lock para operaciones async
        self.write_lock = asyncio.Lock()

        # Inicializar base de datos
        self.init_db()

    def init_db(self):
        """Crear tablas si no existen"""
        with sqlite3.connect(self.db_path) as conn:
            # Tabla de suscripciones
            conn.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    chat_id INTEGER PRIMARY KEY,
                    subscription_type TEXT NOT NULL,
                    threshold_percentage REAL NOT NULL DEFAULT 0.0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Verificar si existe la tabla rate_history con estructura vieja
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='rate_history'")
            table_exists = cursor.fetchone() is not None

            if table_exists:
                # Verificar si tiene la estructura vieja (rate_24h)
                cursor = conn.execute("PRAGMA table_info(rate_history)")
                columns = [row[1] for row in cursor.fetchall()]

                if 'rate_24h' in columns and 'rate_1d' not in columns:
                    logger.info("🔄 Migrando tabla rate_history a nueva estructura...")
                    # Migrar: crear tabla nueva, copiar datos, eliminar vieja, renombrar
                    conn.execute("""
                        CREATE TABLE rate_history_new (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            rate_1d REAL NOT NULL,
                            rate_2d REAL NOT NULL,
                            rate_3d REAL NOT NULL,
                            rate_7d REAL NOT NULL DEFAULT 0,
                            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                    conn.execute("""
                        INSERT INTO rate_history_new (id, rate_1d, rate_2d, rate_3d, rate_7d, timestamp)
                        SELECT id, rate_24h, rate_48h, rate_72h, 0, timestamp FROM rate_history
                    """)
                    conn.execute("DROP TABLE rate_history")
                    conn.execute("ALTER TABLE rate_history_new RENAME TO rate_history")
                    logger.info("✅ Migración completada")
            else:
                # Crear tabla nueva
                conn.execute("""
                    CREATE TABLE rate_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        rate_1d REAL NOT NULL,
                        rate_2d REAL NOT NULL,
                        rate_3d REAL NOT NULL,
                        rate_7d REAL NOT NULL,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # Índice para búsquedas rápidas por fecha
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_rate_history_timestamp
                ON rate_history(timestamp DESC)
            """)

            # Tabla de sugerencias
            conn.execute("""
                CREATE TABLE IF NOT EXISTS suggestions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    username TEXT,
                    message TEXT NOT NULL,
                    read INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.commit()

        logger.info("✅ Base de datos SQLite inicializada")

    def load_subscriptions(self) -> Dict[int, UserSubscription]:
        """Cargar todas las suscripciones desde la base de datos"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                                  SELECT chat_id, subscription_type, threshold_percentage
                                  FROM subscriptions
                                  ORDER BY created_at DESC
                                  """)

            subscriptions = {}
            for row in cursor:
                subscriptions[row['chat_id']] = UserSubscription(
                    chat_id=row['chat_id'],
                    subscription_type=SubscriptionType(row['subscription_type']),
                    threshold_percentage=row['threshold_percentage']
                )

            logger.info(f"✅ Cargadas {len(subscriptions)} suscripciones desde SQLite")
            return subscriptions

    async def save_subscription(self, subscription: UserSubscription):
        """Guardar o actualizar una suscripción (async)"""
        async with self.write_lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO subscriptions 
                    (chat_id, subscription_type, threshold_percentage, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                """, (
                    subscription.chat_id,
                    subscription.subscription_type.value,
                    subscription.threshold_percentage
                ))
                conn.commit()

            logger.debug(f"💾 Suscripción guardada: chat_id={subscription.chat_id}")

    async def delete_subscription(self, chat_id: int):
        """Eliminar una suscripción (async)"""
        async with self.write_lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM subscriptions WHERE chat_id = ?", (chat_id,))
                conn.commit()

            logger.info(f"🗑️ Suscripción eliminada: chat_id={chat_id}")

    def save_rate_history(self, rates: dict):
        """Guardar tasas en la base de datos"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO rate_history (rate_1d, rate_2d, rate_3d, rate_7d, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (rates['1d'], rates['2d'], rates['3d'], rates['7d'], rates['timestamp']))
            conn.commit()
        logger.debug(f"💾 Tasas guardadas en DB: {rates}")

    def get_latest_rates(self) -> dict | None:
        """Obtener las últimas tasas guardadas en la base de datos"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT rate_1d, rate_2d, rate_3d, rate_7d, timestamp
                FROM rate_history
                ORDER BY id DESC
                LIMIT 1
            """)
            row = cursor.fetchone()

            if row:
                return {
                    '1d': row['rate_1d'],
                    '2d': row['rate_2d'],
                    '3d': row['rate_3d'],
                    '7d': row['rate_7d'],
                    'timestamp': row['timestamp']
                }
            return None

    async def save_suggestion(self, chat_id: int, username: str, message: str):
        """Guardar una sugerencia en la base de datos"""
        async with self.write_lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO suggestions (chat_id, username, message)
                    VALUES (?, ?, ?)
                """, (chat_id, username, message))
                conn.commit()
        logger.info(f"💬 Sugerencia guardada de chat_id={chat_id}")

    def get_suggestions(self, unread_only: bool = False) -> list:
        """Obtener sugerencias de la base de datos"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            query = "SELECT * FROM suggestions"
            if unread_only:
                query += " WHERE read = 0"
            query += " ORDER BY created_at DESC LIMIT 20"
            cursor = conn.execute(query)
            return [dict(row) for row in cursor.fetchall()]

    def mark_suggestion_read(self, suggestion_id: int):
        """Marcar una sugerencia como leída"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE suggestions SET read = 1 WHERE id = ?", (suggestion_id,))
            conn.commit()

    def get_stats(self) -> dict:
        """Obtener estadísticas del bot"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                                  SELECT COUNT(*)                                                                      as total_users,
                                         SUM(CASE WHEN subscription_type = 'any_change' THEN 1 ELSE 0 END)             as any_change_users,
                                         SUM(CASE WHEN subscription_type = 'percentage' THEN 1 ELSE 0 END)             as percentage_users,
                                         AVG(CASE WHEN subscription_type = 'percentage' THEN threshold_percentage END) as avg_threshold
                                  FROM subscriptions
                                  """)

            row = cursor.fetchone()
            return {
                'total_users': row[0] or 0,
                'any_change_users': row[1] or 0,
                'percentage_users': row[2] or 0,
                'avg_threshold': round(row[3] or 0, 2)
            }


class CaucionBot:
    def __init__(self, telegram_token: str, ppi_env: Environment, db_path: str = "data/bot.db"):
        self.telegram_token = telegram_token
        self.ppi_config = PPIConfig.from_environment(ppi_env)
        self.ppi_env = ppi_env
        self.ppi = None
        self.subscriptions = {}  # {chat_id: UserSubscription}
        self.last_rates = None  # Últimas tasas obtenidas (en memoria para comparar)
        self.check_interval = 60  # Verificar cada 60 segundos
        self.db_path = db_path

        # Sistema de persistencia SQLite
        self.persistence = SQLitePersistence(db_path=db_path)

        # Cargar suscripciones guardadas
        self.subscriptions = self.persistence.load_subscriptions()

        # Cargar últimas tasas de la DB para tener referencia
        self.last_rates = self.persistence.get_latest_rates()

        logger.info(f"🔄 Bot inicializado con {len(self.subscriptions)} suscripciones")

    async def _save_subscription(self, subscription: UserSubscription):
        """Helper para guardar una suscripción"""
        await self.persistence.save_subscription(subscription)

    async def _delete_subscription(self, chat_id: int):
        """Helper para eliminar una suscripción"""
        await self.persistence.delete_subscription(chat_id)

    def is_market_open(self) -> bool:
        """Verificar si el mercado de cauciones está abierto"""
        now = datetime.now(ARGENTINA_TZ)

        # Verificar si es fin de semana (5 = sábado, 6 = domingo)
        if now.weekday() >= 5:
            return False

        # Verificar horario (10:00 - 17:00)
        market_open = now.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE, second=0, microsecond=0)
        market_close = now.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MINUTE, second=0, microsecond=0)

        return market_open <= now <= market_close

    def connect_ppi(self):
        """Conectar a PPI"""
        try:
            self.ppi = PPI(self.ppi_config.sandbox)
            self.ppi.account.login_api(
                self.ppi_config.public_key,
                self.ppi_config.private_key
            )
            logger.info("Conectado a PPI exitosamente")
            return True
        except Exception as e:
            logger.error(f"Error conectando a PPI: {e}")
            return False

    def get_caucion_rates(self) -> dict:
        """Obtener tasas de cauciones"""
        try:
            rates = {}

            tasa24h = self.ppi.marketdata.current("PESOS1", "CAUCIONES", "INMEDIATA")
            rates['1d'] = float(tasa24h.get('price', 0))

            tasa48h = self.ppi.marketdata.current("PESOS2", "CAUCIONES", "INMEDIATA")
            rates['2d'] = float(tasa48h.get('price', 0))

            tasa72h = self.ppi.marketdata.current("PESOS3", "CAUCIONES", "INMEDIATA")
            rates['3d'] = float(tasa72h.get('price', 0))

            tasa168h = self.ppi.marketdata.current("PESOS7", "CAUCIONES", "INMEDIATA")
            rates['7d'] = float(tasa168h.get('price', 0))

            rates['timestamp'] = datetime.now(ARGENTINA_TZ).strftime("%Y-%m-%d %H:%M:%S")

            return rates
        except Exception as e:
            logger.error(f"Error obteniendo tasas: {e}")
            return None

    def calculate_changes(self, old_rates: dict, new_rates: dict) -> dict:
        """Calcular cambios entre tasas antiguas y nuevas"""
        if not old_rates or not new_rates:
            return None

        changes = {}
        for period in ['1d', '2d', '3d', '7d']:
            old_value = old_rates.get(period, 0)
            new_value = new_rates.get(period, 0)

            if old_value == 0:
                changes[period] = {
                    'absolute': 0,
                    'percentage': 0,
                    'changed': False
                }
            else:
                absolute_change = new_value - old_value
                percentage_change = (absolute_change / old_value) * 100

                changes[period] = {
                    'old': old_value,
                    'new': new_value,
                    'absolute': absolute_change,
                    'percentage': percentage_change,
                    'changed': abs(absolute_change) > 0.001  # Tolerancia para floats
                }

        return changes

    def format_rates_message(self, rates: dict, changes: dict = None, market_closed: bool = False) -> str:
        """Formatear mensaje con las tasas"""
        if not rates:
            return "❌ Error al obtener las tasas de cauciones"

        if market_closed:
            message = "🔒 *MERCADO CERRADO*\n\n"
            message += "📊 *Últimas tasas registradas:*\n\n"
        else:
            message = "📊 *TASAS DE CAUCIONES*\n\n"

        for period, label in [('1d', '🕐'), ('2d', '🕑'), ('3d', '🕒'), ('7d', '🕒')]:
            rate = rates[period]
            message += f"{label} {period.upper()}: `{rate:.2f}%` TNA"

            if changes and period in changes and changes[period]['changed']:
                change = changes[period]
                arrow = "📈" if change['absolute'] > 0 else "📉"
                sign = "+" if change['absolute'] > 0 else ""
                message += f" {arrow} {sign}{change['absolute']:.2f}% ({sign}{change['percentage']:.2f}%)"

            message += "\n"

        message += f"\n🕒 Actualizado: {rates['timestamp']}"

        if market_closed:
            message += "\n\n📅 *Horario del mercado:* Lun-Vie 10:30 - 17:00"

        return message

    def should_notify_user(self, subscription: UserSubscription, changes: dict) -> bool:
        """Determinar si se debe notificar al usuario basado en su configuración"""
        if subscription.subscription_type == SubscriptionType.NONE:
            return False

        if subscription.subscription_type == SubscriptionType.ANY_CHANGE:
            # Notificar si hay cualquier cambio
            return any(changes[period]['changed'] for period in changes)

        if subscription.subscription_type == SubscriptionType.PERCENTAGE:
            # Notificar si algún cambio supera el umbral (en puntos porcentuales absolutos)
            for period in changes:
                if changes[period]['changed']:
                    abs_change = abs(changes[period]['absolute'])
                    if abs_change >= subscription.threshold_percentage:
                        return True
            return False

        return False

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /start"""
        chat_id = update.effective_chat.id
        is_new_user = chat_id not in self.subscriptions

        if is_new_user:
            # Mensaje para usuarios nuevos - más guiado
            welcome_message = (
                "👋 *¡Hola! Soy @caucho_bot*\n\n"
                "Te ayudo a monitorear las tasas de cauciones en tiempo real.\n\n"
                "🎯 *¿Qué puedo hacer por vos?*\n\n"
                "📊 *Ver tasas actuales*\n"
                "Usa /tasas para consultar las tasas de 1 día, 2 días, 3 días y 7 días\n\n"
                "🔔 *Recibir alertas automáticas*\n"
                "Te notifico cuando las tasas cambien. Puedes elegir:\n"
                "  • Cualquier variación\n"
                "  • Solo cambios importantes (>1%, >2%, etc.)\n\n"
                "¿Quieres empezar? Elige una opción:"
            )

            keyboard = [
                [InlineKeyboardButton("📊 Ver tasas actuales", callback_data="quick_tasas")],
                [InlineKeyboardButton("🔔 Configurar alertas", callback_data="quick_config")],
                [InlineKeyboardButton("ℹ️ Ver todos los comandos", callback_data="quick_help")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                welcome_message,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            # Mensaje para usuarios que regresan
            sub = self.subscriptions[chat_id]
            if sub.subscription_type == SubscriptionType.ANY_CHANGE:
                config_info = "🔔 Notificaciones: Cualquier cambio"
            elif sub.subscription_type == SubscriptionType.PERCENTAGE:
                config_info = f"📊 Notificaciones: Cambios > {sub.threshold_percentage}%"
            else:
                config_info = "⏸️ Sin notificaciones activas"

            welcome_back = (
                f"👋 *¡Bienvenido de nuevo!*\n\n"
                f"{config_info}\n\n"
                f"*Acciones rápidas:*\n"
                f"• /tasas - Ver tasas actuales\n"
                f"• /configurar - Cambiar alertas\n"
                f"• /estado - Ver tu configuración\n"
                f"• /pausar - Pausar notificaciones\n"
            )
            await update.message.reply_text(welcome_back, parse_mode='Markdown')

    async def tasas_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /tasas - Mostrar tasas desde la base de datos"""
        # Leer las últimas tasas de la base de datos
        rates = self.persistence.get_latest_rates()

        if not rates:
            await update.message.reply_text(
                "❌ No hay tasas registradas aún.\n\n"
                "El bot registra tasas automáticamente durante el horario de mercado (Lun-Vie 10:30-17:00).",
                parse_mode='Markdown'
            )
            return

        market_closed = not self.is_market_open()
        message = self.format_rates_message(rates, market_closed=market_closed)
        await update.message.reply_text(message, parse_mode='Markdown')

    async def configurar_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /configurar - Mostrar opciones de configuración"""
        keyboard = [
            [
                InlineKeyboardButton("🔔 Cualquier cambio", callback_data="config_any_change")
            ],
            [
                InlineKeyboardButton("📊 Cambio > 0.5%", callback_data="config_0.5"),
                InlineKeyboardButton("📊 Cambio > 1%", callback_data="config_1.0")
            ],
            [
                InlineKeyboardButton("📊 Cambio > 2%", callback_data="config_2.0"),
                InlineKeyboardButton("📊 Cambio > 5%", callback_data="config_5.0")
            ],
            [
                InlineKeyboardButton("⚙️ Personalizado", callback_data="config_custom")
            ]
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)

        message = (
            "⚙️ *Configurar Notificaciones*\n\n"
            "Elige cuándo quieres recibir notificaciones:\n\n"
            "🔔 *Cualquier cambio* - Te notificaré cada vez que las tasas varíen\n\n"
            "📊 *Cambio porcentual* - Solo cuando el cambio supere el % que elijas\n\n"
            "Selecciona una opción:"
        )

        await update.message.reply_text(
            message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    async def estado_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /estado - Mostrar configuración actual"""
        chat_id = update.effective_chat.id

        if chat_id not in self.subscriptions:
            message = "ℹ️ No tienes notificaciones activas.\n\nUsa /configurar para activarlas."
        else:
            sub = self.subscriptions[chat_id]
            if sub.subscription_type == SubscriptionType.ANY_CHANGE:
                message = "✅ *Notificaciones activas*\n\nTipo: 🔔 Cualquier cambio"
            elif sub.subscription_type == SubscriptionType.PERCENTAGE:
                message = f"✅ *Notificaciones activas*\n\nTipo: 📊 Cambio > {sub.threshold_percentage}%"
            else:
                message = "ℹ️ No tienes notificaciones activas."

        await update.message.reply_text(message, parse_mode='Markdown')

    async def pausar_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /pausar - Desactivar notificaciones"""
        chat_id = update.effective_chat.id

        if chat_id in self.subscriptions:
            del self.subscriptions[chat_id]

            # 💾 Eliminar de base de datos
            await self._delete_subscription(chat_id)

            await update.message.reply_text(
                "⏸️ Notificaciones pausadas.\n\nUsa /configurar para reactivarlas."
            )
            logger.info(f"Usuario {chat_id} pausó notificaciones")
        else:
            await update.message.reply_text("ℹ️ No tienes notificaciones activas")

    async def ayuda_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /ayuda"""
        help_message = (
            "ℹ️ *Guía de Uso del Bot*\n\n"
            "*📊 Consultar tasas:*\n"
            "/tasas - Ver las tasas actuales de cauciones 1 día, 2 días, 3 días y 7 días\n\n"
            "*🔔 Configurar alertas:*\n"
            "/configurar - Elegir cuándo recibir notificaciones:\n"
            "  • Cualquier cambio en las tasas\n"
            "  • Solo cambios mayores a 0.5%, 1%, 2%, 5%\n"
            "  • Umbral personalizado\n\n"
            "*📱 Gestionar alertas:*\n"
            "/estado - Ver tu configuración actual\n"
            "/pausar - Desactivar alertas temporalmente\n\n"
            "*💬 Contacto:*\n"
            "/sugerencia - Enviar una sugerencia o comentario\n\n"
            "*💡 ¿Cómo funciona?*\n"
            "El bot verifica las tasas cada minuto. Cuando detecta un cambio, "
            "te notifica solo si cumple con tu configuración.\n\n"
            "*Ejemplo:*\n"
            "Si eliges \"Cambio > 1%\" y la tasa pasa de 35% a 35.4% (+1.14%), "
            "recibirás una alerta. Si cambia a 35.2% (+0.57%), no recibirás nada.\n\n"
            "¿Necesitas ayuda? Envía /start para volver al menú principal"
        )
        await update.message.reply_text(help_message, parse_mode='Markdown')

    async def sugerencia_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /sugerencia - Iniciar flujo para enviar sugerencia"""
        context.user_data['waiting_suggestion'] = True
        await update.message.reply_text(
            "💬 *Enviar Sugerencia*\n\n"
            "Escribí tu mensaje, sugerencia o comentario.\n\n"
            "📝 Puede ser:\n"
            "• Una idea para mejorar el bot\n"
            "• Un problema que encontraste\n"
            "• Cualquier comentario\n\n"
            "Envía tu mensaje:",
            parse_mode='Markdown'
        )

    async def sugerencias_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /sugerencias - Ver sugerencias (solo admin)"""
        ADMIN_CHAT_ID = int(getenv("ADMIN_CHAT_ID", "0"))

        if ADMIN_CHAT_ID != 0 and update.effective_chat.id != ADMIN_CHAT_ID:
            await update.message.reply_text("⛔ Solo el administrador puede usar este comando")
            return

        suggestions = self.persistence.get_suggestions(unread_only=False)

        if not suggestions:
            await update.message.reply_text("📭 No hay sugerencias registradas.")
            return

        message = "💬 *Sugerencias recibidas:*\n\n"
        for s in suggestions[:10]:  # Mostrar últimas 10
            status = "🆕" if not s['read'] else "✓"
            username = f"@{s['username']}" if s['username'] else f"ID:{s['chat_id']}"
            fecha = s['created_at'][:16] if s['created_at'] else ""
            texto = s['message'][:100] + "..." if len(s['message']) > 100 else s['message']
            message += f"{status} *{username}* ({fecha})\n{texto}\n\n"

            # Marcar como leída
            if not s['read']:
                self.persistence.mark_suggestion_read(s['id'])

        await update.message.reply_text(message, parse_mode='Markdown')

    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /stats - Ver estadísticas del bot (solo admin)"""
        ADMIN_CHAT_ID = int(getenv("ADMIN_CHAT_ID", "0"))

        if ADMIN_CHAT_ID == 0:
            # Si no está configurado, permitir al usuario actual (útil para testing)
            pass
        elif update.effective_chat.id != ADMIN_CHAT_ID:
            await update.message.reply_text("⛔ Solo el administrador puede usar este comando")
            return

        try:
            stats = self.persistence.get_stats()

            # Calcular tamaño de la base de datos
            import os
            db_size = 0
            db_path = Path("data/bot.db")
            if db_path.exists():
                db_size = os.path.getsize(db_path) / (1024 * 1024)  # MB

            message = f"""
📊 *Estadísticas del Bot*

👥 Total usuarios: {stats['total_users']}
🔔 Cualquier cambio: {stats['any_change_users']}
📊 Con umbral: {stats['percentage_users']}
📈 Umbral promedio: {stats['avg_threshold']}%

💾 Tamaño DB: {db_size:.2f} MB  
🗄️ Base de datos: SQLite
🚂 Desplegado en: home-server

_Usa /export para descargar backup de la DB_
            """

            await update.message.reply_text(message, parse_mode='Markdown')

        except Exception as e:
            logger.error(f"Error en /stats: {e}")
            await update.message.reply_text(f"❌ Error obteniendo estadísticas: {str(e)}")

    async def export_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /export - Exportar base de datos (solo admin)"""
        ADMIN_CHAT_ID = int(getenv("ADMIN_CHAT_ID", "0"))

        if ADMIN_CHAT_ID == 0:
            pass
        elif update.effective_chat.id != ADMIN_CHAT_ID:
            await update.message.reply_text("⛔ Solo el administrador puede usar este comando")
            return

        try:
            await update.message.reply_text("📦 Creando backup...")

            # Crear backup
            import shutil
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_file = Path(f"backup_{timestamp}.db")

            shutil.copy2("data/bot.db", backup_file)

            # Enviar archivo
            with open(backup_file, 'rb') as f:
                await update.message.reply_document(
                    document=f,
                    filename=f"caucion_bot_backup_{timestamp}.db",
                    caption=f"📦 Backup de la base de datos\n🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )

            # Limpiar archivo temporal
            backup_file.unlink()

            logger.info(f"Backup exportado a usuario {update.effective_chat.id}")

        except Exception as e:
            logger.error(f"Error en /export: {e}")
            await update.message.reply_text(f"❌ Error creando backup: {str(e)}")

    async def restore_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /restore - Restaurar base de datos desde archivo (solo admin)"""
        ADMIN_CHAT_ID = int(getenv("ADMIN_CHAT_ID", "0"))

        if ADMIN_CHAT_ID == 0:
            pass
        elif update.effective_chat.id != ADMIN_CHAT_ID:
            await update.message.reply_text("⛔ Solo el administrador puede usar este comando")
            return

        await update.message.reply_text(
            "📥 *Restaurar base de datos*\n\n"
            "Enviame el archivo `.db` como documento para restaurar la base de datos.\n\n"
            "⚠️ *Atención:* Esto reemplazará la base de datos actual.",
            parse_mode='Markdown'
        )
        context.user_data['awaiting_restore'] = True

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Manejar documentos recibidos para restore"""
        ADMIN_CHAT_ID = int(getenv("ADMIN_CHAT_ID", "0"))

        if update.effective_chat.id != ADMIN_CHAT_ID:
            return

        if not context.user_data.get('awaiting_restore'):
            return

        document = update.message.document
        if not document.file_name.endswith('.db'):
            await update.message.reply_text("❌ El archivo debe ser un `.db`")
            return

        try:
            await update.message.reply_text("⏳ Descargando y restaurando...")

            file = await context.bot.get_file(document.file_id)
            await file.download_to_drive("data/bot.db")

            context.user_data['awaiting_restore'] = False

            # Reinicializar la base de datos
            self.persistence = SQLitePersistence()

            await update.message.reply_text(
                "✅ *Base de datos restaurada exitosamente*\n\n"
                "La base de datos ha sido reemplazada con el archivo recibido.",
                parse_mode='Markdown'
            )
            logger.info(f"Base de datos restaurada por usuario {update.effective_chat.id}")

        except Exception as e:
            logger.error(f"Error en restore: {e}")
            await update.message.reply_text(f"❌ Error restaurando: {str(e)}")

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Manejar callbacks de botones inline"""
        query = update.callback_query
        await query.answer()

        chat_id = query.message.chat_id
        data = query.data

        # Quick actions desde /start
        if data == "quick_tasas":
            # Mostrar tasas desde la base de datos
            rates = self.persistence.get_latest_rates()

            if not rates:
                message = (
                    "❌ No hay tasas registradas aún.\n\n"
                    "El bot registra tasas automáticamente durante el horario de mercado."
                )
            else:
                market_closed = not self.is_market_open()
                message = self.format_rates_message(rates, market_closed=market_closed)
                message += "\n\n💡 *Tip:* Usa /configurar para recibir alertas cuando cambien"

            await query.edit_message_text(message, parse_mode='Markdown')
            return

        elif data == "quick_config":
            # Ir directamente a configuración
            keyboard = [
                [
                    InlineKeyboardButton("🔔 Cualquier cambio", callback_data="config_any_change")
                ],
                [
                    InlineKeyboardButton("📊 Cambio > 0.5%", callback_data="config_0.5"),
                    InlineKeyboardButton("📊 Cambio > 1%", callback_data="config_1.0")
                ],
                [
                    InlineKeyboardButton("📊 Cambio > 2%", callback_data="config_2.0"),
                    InlineKeyboardButton("📊 Cambio > 5%", callback_data="config_5.0")
                ],
                [
                    InlineKeyboardButton("⚙️ Personalizado", callback_data="config_custom")
                ]
            ]

            reply_markup = InlineKeyboardMarkup(keyboard)

            message = (
                "⚙️ *Configurar Alertas*\n\n"
                "Elige cuándo quieres recibir notificaciones:\n\n"
                "🔔 *Cualquier cambio*\n"
                "Te avisaré cada vez que las tasas varíen\n\n"
                "📊 *Cambio porcentual*\n"
                "Solo cuando supere el % que elijas\n\n"
                "Selecciona una opción:"
            )

            await query.edit_message_text(
                message,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return

        elif data == "quick_help":
            # Mostrar ayuda completa
            help_message = (
                "ℹ️ *Guía Completa*\n\n"
                "*Comandos principales:*\n"
                "• /tasas - Ver tasas actuales de cauciones\n"
                "• /configurar - Configurar alertas automáticas\n"
                "• /estado - Ver tu configuración actual\n"
                "• /pausar - Pausar alertas\n"
                "• /ayuda - Volver a ver esta ayuda\n\n"
                "*Tipos de alertas:*\n\n"
                "🔔 *Cualquier cambio*\n"
                "Recibes notificación cada vez que las tasas varíen, sin importar cuánto.\n\n"
                "📊 *Cambio porcentual*\n"
                "Solo te notificamos cuando el cambio supere un porcentaje específico.\n\n"
                "*Ejemplo:*\n"
                "Si configuras \"Cambio > 1%\" y la tasa pasa de 35% a 35.5% (+1.4%), recibirás una alerta. "
                "Si pasa de 35% a 35.2% (+0.57%), no recibirás nada.\n\n"
                "💡 Usa /configurar para empezar"
            )
            await query.edit_message_text(help_message, parse_mode='Markdown')
            return

        # Configuraciones existentes
        if data == "config_any_change":
            # Configurar para notificar en cualquier cambio
            subscription = UserSubscription(
                chat_id=chat_id,
                subscription_type=SubscriptionType.ANY_CHANGE
            )
            self.subscriptions[chat_id] = subscription

            # 💾 Guardar en base de datos
            await self._save_subscription(subscription)

            await query.edit_message_text(
                "✅ *¡Listo!*\n\n"
                "Recibirás una alerta cada vez que las tasas cambien.\n\n"
                "🎯 *Próximos pasos:*\n"
                "• Usa /tasas para ver las tasas actuales\n"
                "• Usa /estado para verificar tu configuración\n"
                "• Usa /pausar si quieres desactivar las alertas\n\n"
                "📊 El bot está monitoreando las tasas cada minuto. Te avisaré cuando cambien.",
                parse_mode='Markdown'
            )
            logger.info(f"Usuario {chat_id} configuró: cualquier cambio")

        elif data.startswith("config_") and data != "config_custom":
            # Configurar umbral porcentual
            percentage = float(data.replace("config_", ""))
            subscription = UserSubscription(
                chat_id=chat_id,
                subscription_type=SubscriptionType.PERCENTAGE,
                threshold_percentage=percentage
            )
            self.subscriptions[chat_id] = subscription

            # 💾 Guardar en base de datos
            await self._save_subscription(subscription)

            await query.edit_message_text(
                f"✅ *¡Listo!*\n\n"
                f"Recibirás alertas cuando las tasas cambien más de {percentage}%\n\n"
                f"🎯 *Próximos pasos:*\n"
                f"• Usa /tasas para ver las tasas actuales\n"
                f"• Usa /estado para verificar tu configuración\n"
                f"• Usa /configurar si quieres cambiar el umbral\n\n"
                f"📊 El bot está monitoreando las tasas cada minuto. Te avisaré cuando cambien más de {percentage}%",
                parse_mode='Markdown'
            )
            logger.info(f"Usuario {chat_id} configuró: cambio > {percentage}%")

        elif data == "config_custom":
            # Configurar umbral personalizado
            await query.edit_message_text(
                "⚙️ *Umbral Personalizado*\n\n"
                "Envía un número con el porcentaje que deseas.\n\n"
                "📝 *Ejemplos:*\n"
                "• `0.5` = Alertas cuando cambie más de 0.5%\n"
                "• `1.5` = Alertas cuando cambie más de 1.5%\n"
                "• `3` = Alertas cuando cambie más de 3%\n\n"
                "Envía tu número:",
                parse_mode='Markdown'
            )
            # Guardar estado para esperar el porcentaje
            context.user_data['waiting_custom_threshold'] = True

    async def _send_welcome_message(self, update: Update):
        """Enviar mensaje de bienvenida (reutilizable)"""
        chat_id = update.effective_chat.id
        is_new_user = chat_id not in self.subscriptions

        if is_new_user:
            welcome_message = (
                "👋 *¡Hola! Soy @caucho_bot*\n\n"
                "Te ayudo a monitorear las tasas de cauciones en tiempo real.\n\n"
                "🎯 *¿Qué puedo hacer por vos?*\n\n"
                "📊 *Ver tasas actuales*\n"
                "Usa /tasas para consultar las tasas de 1 día, 2 días, 3 días y 7 días\n\n"
                "🔔 *Recibir alertas automáticas*\n"
                "Te notifico cuando las tasas cambien. Puedes elegir:\n"
                "  • Cualquier variación\n"
                "  • Solo cambios importantes (>1%, >2%, etc.)\n\n"
                "¿Quieres empezar? Elige una opción:"
            )

            keyboard = [
                [InlineKeyboardButton("📊 Ver tasas actuales", callback_data="quick_tasas")],
                [InlineKeyboardButton("🔔 Configurar alertas", callback_data="quick_config")],
                [InlineKeyboardButton("ℹ️ Ver todos los comandos", callback_data="quick_help")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                welcome_message,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            sub = self.subscriptions[chat_id]
            if sub.subscription_type == SubscriptionType.ANY_CHANGE:
                config_info = "🔔 Notificaciones: Cualquier cambio"
            elif sub.subscription_type == SubscriptionType.PERCENTAGE:
                config_info = f"📊 Notificaciones: Cambios > {sub.threshold_percentage}%"
            else:
                config_info = "⏸️ Sin notificaciones activas"

            welcome_back = (
                f"👋 *¡Hola!*\n\n"
                f"{config_info}\n\n"
                f"*Acciones rápidas:*\n"
                f"• /tasas - Ver tasas actuales\n"
                f"• /configurar - Cambiar alertas\n"
                f"• /estado - Ver tu configuración\n"
                f"• /pausar - Pausar notificaciones\n"
            )
            await update.message.reply_text(welcome_back, parse_mode='Markdown')

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Manejar mensajes de texto (para umbral personalizado o mensajes no reconocidos)"""
        if context.user_data.get('waiting_custom_threshold'):
            try:
                percentage = float(update.message.text.strip().replace(',', '.'))

                if percentage < 0 or percentage > 100:
                    await update.message.reply_text(
                        "❌ El porcentaje debe estar entre 0 y 100.\n\n"
                        "💡 *Tip:* Si quieres alertas frecuentes, usa 0.5 o 1.\n"
                        "Si solo quieres cambios importantes, usa 2 o 5.\n\n"
                        "Intenta de nuevo:",
                        parse_mode='Markdown'
                    )
                    return

                chat_id = update.effective_chat.id
                subscription = UserSubscription(
                    chat_id=chat_id,
                    subscription_type=SubscriptionType.PERCENTAGE,
                    threshold_percentage=percentage
                )
                self.subscriptions[chat_id] = subscription

                # 💾 Guardar en base de datos
                await self._save_subscription(subscription)

                # Dar recomendación basada en el umbral elegido
                if percentage < 0.5:
                    tip = "📊 Umbral muy bajo: Recibirás alertas frecuentes, ideal para trading activo."
                elif percentage < 1:
                    tip = "📊 Umbral bajo: Balance entre detalle y frecuencia."
                elif percentage < 3:
                    tip = "📊 Umbral medio: Solo cambios moderados a significativos."
                else:
                    tip = "📊 Umbral alto: Solo cambios muy importantes."

                await update.message.reply_text(
                    f"✅ *¡Configuración guardada!*\n\n"
                    f"Recibirás alertas cuando las tasas cambien más de {percentage}%\n\n"
                    f"{tip}\n\n"
                    f"🎯 *Próximos pasos:*\n"
                    f"• /tasas - Ver tasas actuales\n"
                    f"• /estado - Verificar configuración\n"
                    f"• /configurar - Cambiar umbral\n\n"
                    f"📊 Ya estoy monitoreando las tasas para vos.",
                    parse_mode='Markdown'
                )

                context.user_data['waiting_custom_threshold'] = False
                logger.info(f"Usuario {chat_id} configuró umbral personalizado: {percentage}%")

            except ValueError:
                await update.message.reply_text(
                    "❌ Por favor envía solo un número.\n\n"
                    "📝 *Ejemplos válidos:*\n"
                    "• 0.5\n"
                    "• 1.5\n"
                    "• 2\n"
                    "• 5\n\n"
                    "Intenta de nuevo:",
                    parse_mode='Markdown'
                )

        elif context.user_data.get('waiting_suggestion'):
            chat_id = update.effective_chat.id
            username = update.effective_user.username
            message_text = update.message.text.strip()

            if len(message_text) < 5:
                await update.message.reply_text(
                    "❌ El mensaje es muy corto.\n\n"
                    "Por favor escribí un mensaje más detallado:",
                    parse_mode='Markdown'
                )
                return

            # Guardar en base de datos
            await self.persistence.save_suggestion(chat_id, username, message_text)

            await update.message.reply_text(
                "✅ *¡Gracias por tu sugerencia!*\n\n"
                "Tu mensaje fue registrado correctamente.\n\n"
                "Aprecio tu feedback para mejorar el bot.",
                parse_mode='Markdown'
            )

            context.user_data['waiting_suggestion'] = False

        else:
            # Mensaje no reconocido - mostrar bienvenida
            await self._send_welcome_message(update)

    async def check_rates_and_notify(self, context: ContextTypes.DEFAULT_TYPE):
        """Verificar tasas periódicamente, guardar en DB y notificar cambios"""
        # No verificar si el mercado está cerrado
        if not self.is_market_open():
            logger.debug("Mercado cerrado - no se verifican tasas")
            return

        if not self.ppi:
            self.connect_ppi()

        # Obtener nuevas tasas de la API
        new_rates = self.get_caucion_rates()

        if not new_rates:
            logger.error("No se pudieron obtener las tasas")
            return

        # Guardar tasas en la base de datos
        self.persistence.save_rate_history(new_rates)

        # Si es la primera vez, solo guardar las tasas
        if not self.last_rates:
            self.last_rates = new_rates
            logger.info("Tasas iniciales guardadas en DB")
            return

        # Calcular cambios
        changes = self.calculate_changes(self.last_rates, new_rates)

        # Verificar si hubo cambios
        has_changes = any(changes[period]['changed'] for period in changes)

        if has_changes:
            logger.info(f"Cambios detectados en las tasas: {changes}")

            # Notificar a usuarios según su configuración
            for chat_id, subscription in list(self.subscriptions.items()):
                if self.should_notify_user(subscription, changes):
                    try:
                        message = "🔔 *¡Cambio en las tasas!*\n\n"
                        message += self.format_rates_message(new_rates, changes)

                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=message,
                            parse_mode='Markdown'
                        )
                        logger.info(f"Notificación enviada a {chat_id}")
                    except Exception as e:
                        logger.error(f"Error enviando notificación a {chat_id}: {e}")
                        # Si el bot fue bloqueado, remover suscripción
                        if "bot was blocked" in str(e).lower():
                            del self.subscriptions[chat_id]

            # Actualizar últimas tasas en memoria
            self.last_rates = new_rates

    async def fetch_closing_rates_job(self, context: ContextTypes.DEFAULT_TYPE):
        """Job programado para obtener las tasas al cierre del mercado (17:00)"""
        logger.info("⏰ Ejecutando consulta de cierre programada (17:00)")

        if not self.ppi:
            self.connect_ppi()

        rates = self.get_caucion_rates()
        if rates:
            self.persistence.save_rate_history(rates)
            self.last_rates = rates
            logger.info(f"✅ Tasas de cierre guardadas en DB: {rates}")

    async def backup_db_to_telegram(self, context: ContextTypes.DEFAULT_TYPE):
        """Job diario: enviar copia de la base de datos al admin por Telegram"""
        import shutil

        ADMIN_CHAT_ID = int(getenv("ADMIN_CHAT_ID", "0"))
        if ADMIN_CHAT_ID == 0:
            logger.warning("Backup no enviado: ADMIN_CHAT_ID no configurado")
            return

        try:
            timestamp = datetime.now(ARGENTINA_TZ).strftime("%Y%m%d_%H%M%S")
            backup_file = Path(f"caucion_bot_backup_{timestamp}.db")
            shutil.copy2(self.persistence.db_path, backup_file)

            with open(backup_file, 'rb') as f:
                await context.bot.send_document(
                    chat_id=ADMIN_CHAT_ID,
                    document=f,
                    filename=backup_file.name,
                    caption=f"📦 Backup automático diario\n🕐 {datetime.now(ARGENTINA_TZ).strftime('%Y-%m-%d %H:%M:%S')}"
                )

            backup_file.unlink()
            logger.info("✅ Backup diario enviado por Telegram")

        except Exception as e:
            logger.error(f"❌ Error en backup diario: {e}")

    async def post_init(self, application: Application):
        """Inicialización post-startup"""
        from datetime import time as dt_time

        # Conectar a PPI al iniciar
        self.connect_ppi()

        # Configurar job para verificar tasas periódicamente
        if application.job_queue:
            application.job_queue.run_repeating(
                self.check_rates_and_notify,
                interval=self.check_interval,  # Cada 60 segundos
                first=10  # Primera ejecución después de 10 segundos
            )
            logger.info(f"JobQueue configurado - verificando tasas cada {self.check_interval} segundos")

            # Job diario a las 17:00 para guardar tasas de cierre
            closing_time = dt_time(
                hour=MARKET_CLOSE_HOUR,
                minute=MARKET_CLOSE_MINUTE,
                tzinfo=ARGENTINA_TZ
            )
            application.job_queue.run_daily(
                self.fetch_closing_rates_job,
                time=closing_time,
                days=(0, 1, 2, 3, 4)  # Lunes a viernes
            )
            logger.info(f"📅 Job de cierre programado para las {MARKET_CLOSE_HOUR}:{MARKET_CLOSE_MINUTE:02d}")

            # Job diario de backup a las 23:00
            backup_time = dt_time(hour=23, minute=0, tzinfo=ARGENTINA_TZ)
            application.job_queue.run_daily(
                self.backup_db_to_telegram,
                time=backup_time
            )
            logger.info("📅 Job de backup diario programado para las 23:00")
        else:
            logger.warning("JobQueue no disponible - las notificaciones automáticas no funcionarán")

    def run(self):
        """Ejecutar el bot"""
        # Crear aplicación
        application = Application.builder().token(self.telegram_token).post_init(self.post_init).build()

        # Agregar handlers de comandos
        application.add_handler(CommandHandler("start", self.start_command))
        application.add_handler(CommandHandler("tasas", self.tasas_command))
        application.add_handler(CommandHandler("configurar", self.configurar_command))
        application.add_handler(CommandHandler("estado", self.estado_command))
        application.add_handler(CommandHandler("pausar", self.pausar_command))
        application.add_handler(CommandHandler("ayuda", self.ayuda_command))
        application.add_handler(CommandHandler("stats", self.stats_command))
        application.add_handler(CommandHandler("export", self.export_command))
        application.add_handler(CommandHandler("sugerencia", self.sugerencia_command))
        application.add_handler(CommandHandler("sugerencias", self.sugerencias_command))
        application.add_handler(CommandHandler("restore", self.restore_command))

        # Agregar handler para botones inline
        application.add_handler(CallbackQueryHandler(self.button_callback))

        # Agregar handler para documentos (restore)
        from telegram.ext import MessageHandler, filters
        application.add_handler(MessageHandler(
            filters.Document.ALL,
            self.handle_document
        ))

        # Agregar handler para mensajes de texto
        application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            self.handle_message
        ))

        # Iniciar bot
        logger.info("Bot iniciado...")
        application.run_polling(allowed_updates=Update.ALL_TYPES)


def main():
    # Asegurar que existe directorio de datos
    Path("data").mkdir(exist_ok=True)

    # Detectar ambiente (dev o production)
    bot_env = getenv("BOT_ENV", "production").lower()
    is_dev = bot_env in ("dev", "development", "sandbox")

    if is_dev:
        logger.info("🔧 Ejecutando en modo DESARROLLO")
        telegram_token = getenv("TELEGRAM_BOT_TOKEN_DEV") or getenv("TELEGRAM_BOT_TOKEN")
        ppi_env = Environment.PRODUCTION
        db_path = "data/bot_dev.db"
    else:
        logger.info("🚀 Ejecutando en modo PRODUCCIÓN")
        telegram_token = getenv("TELEGRAM_BOT_TOKEN")
        ppi_env = Environment.PRODUCTION
        db_path = "data/bot.db"

    if not telegram_token:
        logger.error("TELEGRAM_BOT_TOKEN no configurado en .env")
        return

    # Crear y ejecutar bot
    bot = CaucionBot(
        telegram_token=telegram_token,
        ppi_env=ppi_env,
        db_path=db_path
    )
    bot.run()


if __name__ == '__main__':
    main()