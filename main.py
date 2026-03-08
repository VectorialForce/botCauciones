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
from typing import Dict
import psycopg2
from psycopg2.extras import RealDictCursor
from twitter_bot import TwitterBot

load_dotenv()

# Timezone de Argentina
ARGENTINA_TZ = ZoneInfo("America/Buenos_Aires")

# Horario del mercado de cauciones (hora Argentina)
MARKET_OPEN_HOUR = 10
MARKET_OPEN_MINUTE = 30
MARKET_CLOSE_HOUR = 17
MARKET_CLOSE_MINUTE = 00


# Configurar logging mejorado
LOG_FORMAT = '%(asctime)s | %(levelname)-8s | %(message)s'
LOG_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'

logging.basicConfig(
    format=LOG_FORMAT,
    datefmt=LOG_DATE_FORMAT,
    level=logging.INFO
)

# Reducir logs de librerías externas
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)
logging.getLogger('apscheduler').setLevel(logging.WARNING)

logger = logging.getLogger('CauchoBot')


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


class DatabaseHelper:
    """Helpers para verificar y gestionar la conexión a PostgreSQL"""

    def __init__(self, db_config: dict):
        self.db_config = db_config

    def check_connection(self) -> tuple[bool, str]:
        """
        Verificar si la conexión a la base de datos es válida.
        Retorna (success: bool, message: str)
        """
        try:
            conn = psycopg2.connect(**self.db_config)
            conn.close()
            return True, "Conexión exitosa"
        except psycopg2.OperationalError as e:
            return False, f"Error de conexión: {e}"
        except Exception as e:
            return False, f"Error inesperado: {e}"

    def check_tables_exist(self) -> tuple[bool, list[str]]:
        """
        Verificar que las tablas requeridas existen.
        Retorna (all_exist: bool, missing_tables: list)
        """
        required_tables = ['subscriptions', 'rate_history', 'suggestions']
        missing = []

        try:
            conn = psycopg2.connect(**self.db_config)
            with conn.cursor() as cur:
                for table in required_tables:
                    cur.execute("""
                        SELECT EXISTS (
                            SELECT FROM information_schema.tables
                            WHERE table_name = %s
                        )
                    """, (table,))
                    exists = cur.fetchone()[0]
                    if not exists:
                        missing.append(table)
            conn.close()
            return len(missing) == 0, missing
        except Exception as e:
            logger.error(f"Error verificando tablas: {e}")
            return False, required_tables

    def health_check(self) -> dict:
        """
        Realizar un health check completo de la base de datos.
        Retorna un diccionario con el estado.
        """
        result = {
            'healthy': False,
            'connection': False,
            'tables': False,
            'details': {}
        }

        # Verificar conexión
        conn_ok, conn_msg = self.check_connection()
        result['connection'] = conn_ok
        result['details']['connection'] = conn_msg

        if not conn_ok:
            return result

        # Verificar tablas
        tables_ok, missing = self.check_tables_exist()
        result['tables'] = tables_ok
        result['details']['tables'] = 'OK' if tables_ok else f"Faltan: {missing}"

        # Health check general
        result['healthy'] = conn_ok and tables_ok

        return result

    def get_db_stats(self) -> dict:
        """Obtener estadísticas de la base de datos"""
        try:
            conn = psycopg2.connect(**self.db_config)
            with conn.cursor() as cur:
                stats = {}

                # Contar registros en cada tabla
                for table in ['subscriptions', 'rate_history', 'suggestions']:
                    cur.execute(f"SELECT COUNT(*) FROM {table}")
                    stats[f'{table}_count'] = cur.fetchone()[0]

                # Tamaño de la base de datos
                cur.execute("""
                    SELECT pg_size_pretty(pg_database_size(current_database()))
                """)
                stats['db_size'] = cur.fetchone()[0]

            conn.close()
            return stats
        except Exception as e:
            logger.error(f"Error obteniendo stats de DB: {e}")
            return {}


class PostgreSQLPersistence:
    """Maneja persistencia de datos en PostgreSQL"""

    def __init__(self):
        # Verificar que las variables de entorno estén configuradas
        required_vars = ['DB_HOST', 'DB_USER', 'DB_PASS', 'DB_NAME']
        missing = [var for var in required_vars if not getenv(var)]
        if missing:
            raise EnvironmentError(f"Variables de entorno faltantes: {', '.join(missing)}")

        self.db_config = {
            'host': getenv('DB_HOST'),
            'port': int(getenv('DB_PORT', '5432')),
            'user': getenv('DB_USER'),
            'password': getenv('DB_PASS'),
            'dbname': getenv('DB_NAME')
        }

        # Lock para operaciones async
        self.write_lock = asyncio.Lock()

        # Helper para verificaciones
        self.helper = DatabaseHelper(self.db_config)

        # Conexión persistente (None hasta que se use)
        self._conn = None

        # Verificar conexión antes de inicializar
        self._verify_connection()

        # Inicializar base de datos
        self.init_db()

    def _verify_connection(self):
        """Verificar que la conexión es válida antes de continuar"""
        ok, msg = self.helper.check_connection()
        if not ok:
            logger.error(f"❌ No se pudo conectar a PostgreSQL: {msg}")
            raise ConnectionError(f"No se pudo conectar a la base de datos: {msg}")
        logger.info(f"✅ Conexión a PostgreSQL verificada: {self.db_config['host']}")

    def _get_connection(self):
        """Obtener una conexión a la base de datos"""
        try:
            return psycopg2.connect(**self.db_config)
        except psycopg2.OperationalError as e:
            logger.error(f"Error obteniendo conexión: {e}")
            raise

    def close(self):
        """Cerrar conexiones abiertas"""
        if self._conn is not None:
            try:
                self._conn.close()
                self._conn = None
                logger.info("🔒 Conexión a PostgreSQL cerrada")
            except Exception as e:
                logger.error(f"Error cerrando conexión: {e}")

    def init_db(self):
        """Crear tablas si no existen"""
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                # Tabla de suscripciones
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS subscriptions (
                        chat_id BIGINT PRIMARY KEY,
                        subscription_type TEXT NOT NULL,
                        threshold_percentage REAL NOT NULL DEFAULT 0.0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # Tabla de historial de tasas
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS rate_history (
                        id SERIAL PRIMARY KEY,
                        rate_1d REAL NOT NULL,
                        rate_2d REAL NOT NULL,
                        rate_3d REAL NOT NULL,
                        rate_7d REAL NOT NULL,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # Índice para búsquedas rápidas por fecha
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_rate_history_timestamp
                    ON rate_history(timestamp DESC)
                """)

                # Tabla de sugerencias
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS suggestions (
                        id SERIAL PRIMARY KEY,
                        chat_id BIGINT NOT NULL,
                        username TEXT,
                        message TEXT NOT NULL,
                        read BOOLEAN DEFAULT FALSE,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                conn.commit()

        logger.info("✅ Base de datos PostgreSQL inicializada")

    def load_subscriptions(self) -> Dict[int, UserSubscription]:
        """Cargar todas las suscripciones desde la base de datos"""
        with self._get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT chat_id, subscription_type, threshold_percentage
                    FROM subscriptions
                    ORDER BY created_at DESC
                """)

                subscriptions = {}
                for row in cur.fetchall():
                    subscriptions[row['chat_id']] = UserSubscription(
                        chat_id=row['chat_id'],
                        subscription_type=SubscriptionType(row['subscription_type']),
                        threshold_percentage=row['threshold_percentage']
                    )

                logger.info(f"✅ Cargadas {len(subscriptions)} suscripciones desde PostgreSQL")
                return subscriptions

    async def save_subscription(self, subscription: UserSubscription):
        """Guardar o actualizar una suscripción (async)"""
        async with self.write_lock:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO subscriptions
                        (chat_id, subscription_type, threshold_percentage, updated_at)
                        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (chat_id) DO UPDATE SET
                            subscription_type = EXCLUDED.subscription_type,
                            threshold_percentage = EXCLUDED.threshold_percentage,
                            updated_at = CURRENT_TIMESTAMP
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
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM subscriptions WHERE chat_id = %s", (chat_id,))
                    conn.commit()

            logger.info(f"🗑️ Suscripción eliminada: chat_id={chat_id}")

    def save_rate_history(self, rates: dict):
        """Guardar tasas en la base de datos"""
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO rate_history (rate_1d, rate_2d, rate_3d, rate_7d, timestamp)
                    VALUES (%s, %s, %s, %s, %s)
                """, (rates['1d'], rates['2d'], rates['3d'], rates['7d'], rates['timestamp']))
                conn.commit()
        logger.debug(f"💾 Tasas guardadas en DB: {rates}")

    def get_latest_rates(self) -> dict | None:
        """Obtener las últimas tasas guardadas en la base de datos"""
        with self._get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT rate_1d, rate_2d, rate_3d, rate_7d, timestamp
                    FROM rate_history
                    ORDER BY id DESC
                    LIMIT 1
                """)
                row = cur.fetchone()

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
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO suggestions (chat_id, username, message)
                        VALUES (%s, %s, %s)
                    """, (chat_id, username, message))
                    conn.commit()
        logger.info(f"💬 Sugerencia guardada de chat_id={chat_id}")

    def get_suggestions(self, unread_only: bool = False) -> list:
        """Obtener sugerencias de la base de datos"""
        with self._get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                query = "SELECT * FROM suggestions"
                if unread_only:
                    query += " WHERE read = FALSE"
                query += " ORDER BY created_at DESC LIMIT 20"
                cur.execute(query)
                return [dict(row) for row in cur.fetchall()]

    def mark_suggestion_read(self, suggestion_id: int):
        """Marcar una sugerencia como leída"""
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE suggestions SET read = TRUE WHERE id = %s", (suggestion_id,))
                conn.commit()

    def get_stats(self) -> dict:
        """Obtener estadísticas del bot"""
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) as total_users,
                           SUM(CASE WHEN subscription_type = 'any_change' THEN 1 ELSE 0 END) as any_change_users,
                           SUM(CASE WHEN subscription_type = 'percentage' THEN 1 ELSE 0 END) as percentage_users,
                           AVG(CASE WHEN subscription_type = 'percentage' THEN threshold_percentage END) as avg_threshold
                    FROM subscriptions
                """)

                row = cur.fetchone()
                return {
                    'total_users': row[0] or 0,
                    'any_change_users': row[1] or 0,
                    'percentage_users': row[2] or 0,
                    'avg_threshold': round(row[3] or 0, 2) if row[3] else 0
                }


class CaucionBot:
    def __init__(self, telegram_token: str, ppi_env: Environment):
        self.telegram_token = telegram_token
        self.ppi_config = PPIConfig.from_environment(ppi_env)
        self.ppi_env = ppi_env
        self.ppi = None
        self.subscriptions = {}  # {chat_id: UserSubscription}
        self.last_rates = None  # Últimas tasas obtenidas (en memoria para comparar)
        self.check_interval = 60  # Verificar cada 60 segundos
        self.start_time = datetime.now(ARGENTINA_TZ)

        # Estadísticas de sesión
        self.stats = {
            'checks': 0,
            'changes_detected': 0,
            'notifications_sent': 0,
            'notification_errors': 0,
            'api_errors': 0,
            'commands_processed': 0
        }

        # Sistema de persistencia PostgreSQL
        logger.info("[INIT] Conectando a PostgreSQL...")
        self.persistence = PostgreSQLPersistence()

        # Cargar suscripciones guardadas
        self.subscriptions = self.persistence.load_subscriptions()

        # Cargar últimas tasas de la DB para tener referencia
        self.last_rates = self.persistence.get_latest_rates()

        # Integración Twitter (Selenium)
        self.twitter_enabled = getenv("TWITTER_ENABLED", "false").lower() == "true"
        self.twitter_bot = None
        if self.twitter_enabled:
            self.twitter_bot = TwitterBot()
            logger.info("[INIT] Integración Twitter habilitada")

        logger.info("=" * 50)
        logger.info("[INIT] Bot CauchoCauciones inicializado")
        logger.info(f"[INIT] Suscriptores activos: {len(self.subscriptions)}")
        if self.last_rates:
            logger.info(f"[INIT] Última tasa 1D: {self.last_rates['1d']:.2f}%")
        logger.info("=" * 50)

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
                message += f" {arrow} {sign}{change['absolute']:.2f}%"

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

    def _log_command(self, command: str, chat_id: int, extra: str = ""):
        """Helper para loguear comandos"""
        self.stats['commands_processed'] += 1
        extra_info = f" | {extra}" if extra else ""
        logger.info(f"[CMD] /{command} | user={chat_id}{extra_info}")

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /start"""
        chat_id = update.effective_chat.id
        is_new_user = chat_id not in self.subscriptions
        self._log_command("start", chat_id, "nuevo" if is_new_user else "existente")

        if is_new_user:
            # Mensaje para usuarios nuevos - más guiado
            welcome_message = (
                "👋 *¡Hola! Soy @caucho_bot*\n\n"
                "Te ayudo a monitorear las tasas de cauciones en tiempo real.\n\n"
                "🎯 *¿Qué puedo hacer por vos?*\n\n"
                "📊 *Ver tasas actuales*\n"
                "Usa /tasas para consultar las tasas de 1 día, 2 días, 3 días y 7 días\n\n"
                "🔔 *Recibir alertas automáticas*\n"
                "Te notifico cuando las tasas cambien. Podes elegir:\n"
                "  • Cualquier variación\n"
                "  • Solo cambios importantes (>1%, >2%, etc.)\n\n"
                "¿Queres empezar? Elige una opción:"
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
                f"• /sugerencia - Enviar comentario\n"
            )
            await update.message.reply_text(welcome_back, parse_mode='Markdown')

    async def tasas_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /tasas - Mostrar tasas desde la base de datos"""
        self._log_command("tasas", update.effective_chat.id)
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
        self._log_command("configurar", update.effective_chat.id)
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
            "Elegi cuándo queres recibir notificaciones:\n\n"
            "🔔 *Cualquier cambio* - Te voy a notificar cada vez que las tasas varíen\n\n"
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
            message = "ℹ️ No tenes notificaciones activas.\n\nUsa /configurar para activarlas."
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
            logger.info(f"[CONFIG] user={chat_id} | tipo=pausado")
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
            "Si elegis \"Cambio > 1%\" y la tasa pasa de 35% a 35.4% (+1.14%), "
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
            db_stats = self.persistence.helper.get_db_stats()
            health = self.persistence.helper.health_check()

            health_icon = "✅" if health['healthy'] else "❌"

            message = f"""
📊 *Estadísticas del Bot*

👥 Total usuarios: {stats['total_users']}
🔔 Cualquier cambio: {stats['any_change_users']}
📊 Con umbral: {stats['percentage_users']}
📈 Umbral promedio: {stats['avg_threshold']}%

🗄️ *Base de datos:* PostgreSQL
{health_icon} Estado: {'Saludable' if health['healthy'] else 'Con problemas'}
💾 Tamaño: {db_stats.get('db_size', 'N/A')}
📝 Registros tasas: {db_stats.get('rate_history_count', 'N/A')}

🚂 Desplegado en: home-server
            """

            await update.message.reply_text(message, parse_mode='Markdown')

        except Exception as e:
            logger.error(f"Error en /stats: {e}")
            await update.message.reply_text(f"❌ Error obteniendo estadísticas: {str(e)}")

    async def dbstatus_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /dbstatus - Verificar estado de la base de datos (solo admin)"""
        ADMIN_CHAT_ID = int(getenv("ADMIN_CHAT_ID", "0"))

        if ADMIN_CHAT_ID == 0:
            pass
        elif update.effective_chat.id != ADMIN_CHAT_ID:
            await update.message.reply_text("⛔ Solo el administrador puede usar este comando")
            return

        try:
            health = self.persistence.helper.health_check()

            conn_icon = "✅" if health['connection'] else "❌"
            tables_icon = "✅" if health['tables'] else "❌"
            health_icon = "✅" if health['healthy'] else "❌"

            message = f"""
🔍 *Estado de la Base de Datos*

{health_icon} *Estado general:* {'Saludable' if health['healthy'] else 'Con problemas'}

*Detalles:*
{conn_icon} Conexión: {health['details'].get('connection', 'N/A')}
{tables_icon} Tablas: {health['details'].get('tables', 'N/A')}

*Configuración:*
🖥️ Host: `{self.persistence.db_config['host']}`
🔌 Puerto: `{self.persistence.db_config['port']}`
📦 DB: `{self.persistence.db_config['dbname']}`
            """

            await update.message.reply_text(message, parse_mode='Markdown')

        except Exception as e:
            logger.error(f"Error en /dbstatus: {e}")
            await update.message.reply_text(f"❌ Error verificando DB: {str(e)}")

    async def broadcast_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /broadcast - Enviar mensaje a todos los suscriptores (solo admin)"""
        ADMIN_CHAT_ID = int(getenv("ADMIN_CHAT_ID", "0"))

        if ADMIN_CHAT_ID == 0 or update.effective_chat.id != ADMIN_CHAT_ID:
            await update.message.reply_text("⛔ Solo el administrador puede usar este comando")
            return

        # Verificar si hay mensaje
        if not context.args:
            await update.message.reply_text(
                "📢 *Broadcast*\n\n"
                "Uso: `/broadcast <mensaje>`\n\n"
                "Ejemplo:\n"
                "`/broadcast Hola a todos! El bot estará en mantenimiento mañana.`",
                parse_mode='Markdown'
            )
            return

        message_text = ' '.join(context.args)

        await update.message.reply_text(
            f"📤 Enviando mensaje a {len(self.subscriptions)} suscriptores..."
        )

        sent = 0
        failed = 0
        blocked = []

        for chat_id in list(self.subscriptions.keys()):
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"📢 Mensaje del administrador:\n\n{message_text}"
                )
                sent += 1
                # Pequeña pausa para evitar rate limiting
                await asyncio.sleep(0.05)
            except Exception as e:
                failed += 1
                if "bot was blocked" in str(e).lower():
                    blocked.append(chat_id)
                logger.warning(f"[BROADCAST] Error enviando a {chat_id}: {e}")

        # Eliminar usuarios que bloquearon el bot
        for chat_id in blocked:
            if chat_id in self.subscriptions:
                del self.subscriptions[chat_id]
                await self.persistence.delete_subscription(chat_id)

        logger.info(f"[BROADCAST] Enviados: {sent} | Fallidos: {failed} | Bloqueados: {len(blocked)}")

        await update.message.reply_text(
            f"✅ *Broadcast completado*\n\n"
            f"📤 Enviados: {sent}\n"
            f"❌ Fallidos: {failed}\n"
            f"🚫 Bloqueados (removidos): {len(blocked)}",
            parse_mode='Markdown'
        )

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
                "• /sugerencia - Enviar comentario o idea\n"
                "• /ayuda - Volver a ver esta ayuda\n\n"
                "*Tipos de alertas:*\n\n"
                "🔔 *Cualquier cambio*\n"
                "Recibis notificaciones cada vez que las tasas varíen, sin importar cuánto.\n\n"
                "📊 *Cambio porcentual*\n"
                "Solo te notificamos cuando el cambio supere un porcentaje específico.\n\n"
                "*Ejemplo:*\n"
                "Si configuras \"Cambio > 1%\" y la tasa pasa de 35% a 35.5% (+1.4%), vas a recibir una alerta. "
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
            logger.info(f"[CONFIG] user={chat_id} | tipo=cualquier_cambio")

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
                f"Vas a recibir alertas cuando las tasas cambien más de {percentage}%\n\n"
                f"🎯 *Próximos pasos:*\n"
                f"• Usa /tasas para ver las tasas actuales\n"
                f"• Usa /estado para verificar tu configuración\n"
                f"• Usa /configurar si queres cambiar el umbral\n\n"
                f"📊 El bot está monitoreando las tasas cada minuto. Te voy a avisar cuando cambien más de {percentage}%",
                parse_mode='Markdown'
            )
            logger.info(f"[CONFIG] user={chat_id} | tipo=porcentaje | umbral={percentage}%")

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
                f"• /sugerencia - Enviar comentario\n"
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
                    f"Vas a recibir alertas cuando las tasas cambien más de {percentage}%\n\n"
                    f"{tip}\n\n"
                    f"🎯 *Próximos pasos:*\n"
                    f"• /tasas - Ver tasas actuales\n"
                    f"• /estado - Verificar configuración\n"
                    f"• /configurar - Cambiar umbral\n\n"
                    f"📊 Ya estoy monitoreando las tasas para vos.",
                    parse_mode='Markdown'
                )

                context.user_data['waiting_custom_threshold'] = False
                logger.info(f"[CONFIG] user={chat_id} | tipo=personalizado | umbral={percentage}%")

            except ValueError:
                await update.message.reply_text(
                    "❌ Por favor envia solo un número.\n\n"
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
            return

        if not self.ppi:
            self.connect_ppi()

        # Obtener nuevas tasas de la API
        new_rates = self.get_caucion_rates()

        if not new_rates:
            logger.error("[TASAS] Error obteniendo tasas de PPI")
            self.stats['api_errors'] += 1
            return

        # Guardar tasas en la base de datos
        self.persistence.save_rate_history(new_rates)
        self.stats['checks'] += 1

        # Si es la primera vez, solo guardar las tasas
        if not self.last_rates:
            self.last_rates = new_rates
            logger.info(f"[TASAS] Iniciales: 1D={new_rates['1d']:.2f}% | 7D={new_rates['7d']:.2f}%")
            return

        # Calcular cambios
        changes = self.calculate_changes(self.last_rates, new_rates)

        # Verificar si hubo cambios
        has_changes = any(changes[period]['changed'] for period in changes)

        if has_changes:
            self.stats['changes_detected'] += 1

            # Log detallado de cambios
            changes_summary = []
            for period in ['1d', '2d', '3d', '7d']:
                if changes[period]['changed']:
                    diff = changes[period]['absolute']
                    sign = '+' if diff > 0 else ''
                    changes_summary.append(f"{period.upper()}:{sign}{diff:.2f}%")

            logger.info(f"[TASAS] Cambio detectado: {' | '.join(changes_summary)}")

            # Publicar en Twitter si corresponde
            if self.twitter_bot and self.twitter_bot.should_tweet(changes):
                try:
                    tweet_text = self.twitter_bot.format_tweet(new_rates, changes)
                    self.twitter_bot.tweet(tweet_text)
                except Exception as e:
                    logger.error(f"[TWITTER] Error en publicación: {e}")

            # Notificar a usuarios según su configuración
            notified_count = 0
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
                        notified_count += 1
                        self.stats['notifications_sent'] += 1
                    except Exception as e:
                        logger.warning(f"[NOTIFY] Error enviando a {chat_id}: {e}")
                        self.stats['notification_errors'] += 1
                        if "bot was blocked" in str(e).lower():
                            del self.subscriptions[chat_id]
                            logger.info(f"[USERS] Usuario {chat_id} removido (bot bloqueado)")

            if notified_count > 0:
                logger.info(f"[NOTIFY] {notified_count} notificaciones enviadas")

            # Actualizar últimas tasas en memoria
            self.last_rates = new_rates

    async def fetch_closing_rates_job(self, context: ContextTypes.DEFAULT_TYPE):
        """Job programado para obtener las tasas al cierre del mercado (17:00)"""
        logger.info("[JOB] Ejecutando consulta de cierre (17:00)")

        if not self.ppi:
            self.connect_ppi()

        rates = self.get_caucion_rates()
        if rates:
            self.persistence.save_rate_history(rates)
            self.last_rates = rates
            logger.info(f"[JOB] Tasas de cierre: 1D={rates['1d']:.2f}% | 7D={rates['7d']:.2f}%")

    async def log_status_job(self, context: ContextTypes.DEFAULT_TYPE):
        """Job periódico para loguear el estado del bot"""
        uptime = datetime.now(ARGENTINA_TZ) - self.start_time
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes, _ = divmod(remainder, 60)

        market_status = "ABIERTO" if self.is_market_open() else "CERRADO"

        # Resumen de tasas actuales
        rates_info = "N/A"
        if self.last_rates:
            rates_info = f"1D={self.last_rates['1d']:.2f}%"

        logger.info(
            f"[STATUS] Uptime: {hours}h{minutes}m | "
            f"Mercado: {market_status} | "
            f"Subs: {len(self.subscriptions)} | "
            f"Checks: {self.stats['checks']} | "
            f"Cambios: {self.stats['changes_detected']} | "
            f"Notif: {self.stats['notifications_sent']} | "
            f"Tasa: {rates_info}"
        )

    async def post_init(self, application: Application):
        """Inicialización post-startup"""
        from datetime import time as dt_time

        # Conectar a PPI al iniciar
        self.connect_ppi()

        # Configurar job para verificar tasas periódicamente
        if application.job_queue:
            # Job de verificación de tasas (cada 60 segundos)
            application.job_queue.run_repeating(
                self.check_rates_and_notify,
                interval=self.check_interval,
                first=10
            )

            # Job de status (cada 15 minutos)
            application.job_queue.run_repeating(
                self.log_status_job,
                interval=900,  # 15 minutos
                first=60
            )

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

            logger.info("[JOBS] Configurados: tasas(60s), status(15m), cierre(17:00)")
            logger.info(f"[JOBS] Mercado: {MARKET_OPEN_HOUR}:{MARKET_OPEN_MINUTE:02d} - {MARKET_CLOSE_HOUR}:{MARKET_CLOSE_MINUTE:02d}")
            logger.info(f"📅 Job de cierre programado para las {MARKET_CLOSE_HOUR}:{MARKET_CLOSE_MINUTE:02d}")
        else:
            logger.warning("JobQueue no disponible - las notificaciones automáticas no funcionarán")

    async def post_shutdown(self, application: Application):
        """Cleanup al cerrar el bot"""
        logger.info("🛑 Cerrando bot...")
        self.persistence.close()
        logger.info("✅ Bot cerrado correctamente")

    def run(self):
        """Ejecutar el bot"""
        # Crear aplicación con post_init y post_shutdown
        application = (
            Application.builder()
            .token(self.telegram_token)
            .post_init(self.post_init)
            .post_shutdown(self.post_shutdown)
            .build()
        )

        # Agregar handlers de comandos
        application.add_handler(CommandHandler("start", self.start_command))
        application.add_handler(CommandHandler("tasas", self.tasas_command))
        application.add_handler(CommandHandler("configurar", self.configurar_command))
        application.add_handler(CommandHandler("estado", self.estado_command))
        application.add_handler(CommandHandler("pausar", self.pausar_command))
        application.add_handler(CommandHandler("ayuda", self.ayuda_command))
        application.add_handler(CommandHandler("stats", self.stats_command))
        application.add_handler(CommandHandler("dbstatus", self.dbstatus_command))
        application.add_handler(CommandHandler("sugerencia", self.sugerencia_command))
        application.add_handler(CommandHandler("sugerencias", self.sugerencias_command))
        application.add_handler(CommandHandler("broadcast", self.broadcast_command))

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
    # Detectar ambiente (dev o production)
    bot_env = getenv("BOT_ENV", "production").lower()
    is_dev = bot_env in ("dev", "development", "sandbox")

    if is_dev:
        logger.info("🔧 Ejecutando en modo DESARROLLO")
        telegram_token = getenv("TELEGRAM_BOT_TOKEN_DEV") or getenv("TELEGRAM_BOT_TOKEN")
        ppi_env = Environment.PRODUCTION
    else:
        logger.info("🚀 Ejecutando en modo PRODUCCIÓN")
        telegram_token = getenv("TELEGRAM_BOT_TOKEN")
        ppi_env = Environment.PRODUCTION

    if not telegram_token:
        logger.error("TELEGRAM_BOT_TOKEN no configurado en .env")
        return

    # Crear y ejecutar bot
    bot = CaucionBot(
        telegram_token=telegram_token,
        ppi_env=ppi_env
    )
    bot.run()


if __name__ == '__main__':
    main()