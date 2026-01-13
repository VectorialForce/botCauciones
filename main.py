from enum import Enum
from ppi_client.ppi import PPI
from datetime import datetime
from dotenv import load_dotenv
from os import getenv
from dataclasses import dataclass
import asyncio
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

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


class CaucionBot:
    def __init__(self, telegram_token: str, ppi_env: Environment):
        self.telegram_token = telegram_token
        self.ppi_config = PPIConfig.from_environment(ppi_env)
        self.ppi = None
        self.subscribers = set()  # Chat IDs de usuarios suscritos

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
            rates['24h'] = tasa24h.get('price', 'N/A')

            tasa48h = self.ppi.marketdata.current("PESOS2", "CAUCIONES", "INMEDIATA")
            rates['48h'] = tasa48h.get('price', 'N/A')

            tasa72h = self.ppi.marketdata.current("PESOS3", "CAUCIONES", "INMEDIATA")
            rates['72h'] = tasa72h.get('price', 'N/A')

            rates['timestamp'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            return rates
        except Exception as e:
            logger.error(f"Error obteniendo tasas: {e}")
            return None

    def format_rates_message(self, rates: dict) -> str:
        """Formatear mensaje con las tasas"""
        if not rates:
            return "❌ Error al obtener las tasas de cauciones"

        message = "📊 *TASAS DE CAUCIONES*\n\n"
        message += f"🕐 24 horas: `{rates['24h']}%` TNA\n"
        message += f"🕑 48 horas: `{rates['48h']}%` TNA\n"
        message += f"🕒 72 horas: `{rates['72h']}%` TNA\n\n"
        message += f"🕒 Actualizado: {rates['timestamp']}"

        return message

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /start"""
        welcome_message = (
            "👋 ¡Bienvenido al Bot de Tasas de Cauciones!\n\n"
            "Comandos disponibles:\n"
            "/tasas - Ver tasas actuales\n"
            "/suscribir - Recibir actualizaciones automáticas cada 5 minutos\n"
            "/pausar - Pausar actualizaciones automáticas\n"
            "/ayuda - Ver esta ayuda"
        )
        await update.message.reply_text(welcome_message)

    async def tasas_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /tasas - Mostrar tasas actuales"""
        await update.message.reply_text("🔄 Obteniendo tasas...")

        if not self.ppi:
            self.connect_ppi()

        rates = self.get_caucion_rates()
        message = self.format_rates_message(rates)

        await update.message.reply_text(message, parse_mode='Markdown')

    async def suscribir_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /suscribir - Activar actualizaciones automáticas"""
        chat_id = update.effective_chat.id

        if chat_id in self.subscribers:
            await update.message.reply_text("✅ Ya estás suscrito a las actualizaciones automáticas")
        else:
            self.subscribers.add(chat_id)
            await update.message.reply_text(
                "✅ ¡Suscripción activada!\n"
                "Recibirás actualizaciones de tasas cada 5 minutos.\n"
                "Usa /pausar para detener las notificaciones."
            )
            logger.info(f"Usuario {chat_id} suscrito")

    async def pausar_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /pausar - Desactivar actualizaciones automáticas"""
        chat_id = update.effective_chat.id

        if chat_id in self.subscribers:
            self.subscribers.remove(chat_id)
            await update.message.reply_text("⏸️ Actualizaciones pausadas. Usa /suscribir para reactivarlas.")
            logger.info(f"Usuario {chat_id} pausado")
        else:
            await update.message.reply_text("ℹ️ No estás suscrito a las actualizaciones")

    async def ayuda_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /ayuda"""
        await self.start_command(update, context)

    async def send_rates_to_subscribers(self, context: ContextTypes.DEFAULT_TYPE):
        """Enviar tasas a todos los suscriptores (tarea periódica)"""
        if not self.subscribers:
            return

        logger.info(f"Enviando tasas a {len(self.subscribers)} suscriptores")

        if not self.ppi:
            self.connect_ppi()

        rates = self.get_caucion_rates()
        message = self.format_rates_message(rates)

        # Enviar a todos los suscriptores
        failed_subscribers = []
        for chat_id in self.subscribers:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.error(f"Error enviando mensaje a {chat_id}: {e}")
                failed_subscribers.append(chat_id)

        # Remover suscriptores que fallaron (posiblemente bloquearon el bot)
        for chat_id in failed_subscribers:
            self.subscribers.discard(chat_id)

    async def post_init(self, application: Application):
        """Inicialización post-startup"""
        # Conectar a PPI al iniciar
        self.connect_ppi()

        # Configurar job para enviar actualizaciones cada 5 minutos
        application.job_queue.run_repeating(
            self.send_rates_to_subscribers,
            interval=300,  # 5 minutos (300 segundos)
            first=10  # Primera ejecución después de 10 segundos
        )

    def run(self):
        """Ejecutar el bot"""
        # Crear aplicación
        application = Application.builder().token(self.telegram_token).post_init(self.post_init).build()

        # Agregar handlers de comandos
        application.add_handler(CommandHandler("start", self.start_command))
        application.add_handler(CommandHandler("tasas", self.tasas_command))
        application.add_handler(CommandHandler("suscribir", self.suscribir_command))
        application.add_handler(CommandHandler("pausar", self.pausar_command))
        application.add_handler(CommandHandler("ayuda", self.ayuda_command))

        # Iniciar bot
        logger.info("Bot iniciado...")
        application.run_polling(allowed_updates=Update.ALL_TYPES)


def main():
    # Obtener token de Telegram desde variables de entorno
    telegram_token = getenv("TELEGRAM_API_KEY")

    if not telegram_token:
        logger.error("TELEGRAM_BOT_TOKEN no configurado en .env")
        return

    # Crear y ejecutar bot
    bot = CaucionBot(
        telegram_token=telegram_token,
        ppi_env=Environment.PRODUCTION
    )
    bot.run()


if __name__ == '__main__':
    main()