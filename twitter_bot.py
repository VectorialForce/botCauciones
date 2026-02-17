from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium_stealth import stealth
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from os import getenv
from pathlib import Path
import logging
import time

load_dotenv()

logger = logging.getLogger('CauchoBot')

ARGENTINA_TZ = ZoneInfo("America/Buenos_Aires")

TWEET_THRESHOLD = 5.0  # Puntos porcentuales absolutos


class TwitterBot:
    def __init__(self, chrome_profile_path: str = None):
        self.chrome_profile_path = chrome_profile_path or getenv("CHROME_PROFILE_PATH", "chrome_profile")
        self.driver = None

    def _init_driver(self):
        """Inicializar ChromeDriver con stealth"""
        options = webdriver.ChromeOptions()

        # Usar Chromium si está disponible (Docker ARM64), sino Chrome local
        service = None
        chromium_path = Path("/usr/bin/chromium")
        chromedriver_path = Path("/usr/bin/chromedriver")
        if chromium_path.exists():
            options.binary_location = str(chromium_path)
        if chromedriver_path.exists():
            service = Service(str(chromedriver_path))

        # Usar profile con sesión de Twitter pre-logueada
        profile_abs = str(Path(self.chrome_profile_path).resolve())
        options.add_argument(f"--user-data-dir={profile_abs}")

        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")

        self.driver = webdriver.Chrome(options=options, service=service) if service else webdriver.Chrome(options=options)

        stealth(
            self.driver,
            languages=["es-AR", "es"],
            vendor="Google Inc.",
            platform="Win32",
            webgl_vendor="Intel Inc.",
            renderer="Intel Iris OpenGL Engine",
            fix_hairline=True,
        )

        logger.info("[TWITTER] ChromeDriver inicializado con stealth")

    def should_tweet(self, changes: dict) -> bool:
        """Determinar si se debe twittear (suba >= 5pp en cualquier período)"""
        if not changes:
            return False

        for period in changes:
            if changes[period]['changed']:
                if changes[period]['absolute'] >= TWEET_THRESHOLD:
                    return True
        return False

    def format_tweet(self, rates: dict, changes: dict) -> str:
        """Formatear mensaje para Twitter (plain-text, sin markdown)"""
        message = "🔔 ¡Cambio en las tasas!\n\n"
        message += "📊 TASAS DE CAUCIONES\n\n"

        for period, label in [('1d', '🕐'), ('2d', '🕑'), ('3d', '🕒'), ('7d', '🕒')]:
            rate = rates[period]
            message += f"{label} {period.upper()}: {rate:.2f}% TNA"

            if changes and period in changes and changes[period]['changed']:
                change = changes[period]
                arrow = "📈" if change['absolute'] > 0 else "📉"
                sign = "+" if change['absolute'] > 0 else ""
                message += f" {arrow} {sign}{change['absolute']:.2f}% ({sign}{change['percentage']:.2f}%)"

            message += "\n"

        message += f"\n🕒 Actualizado: {rates['timestamp']}"

        return message

    def tweet(self, text: str) -> bool:
        """Publicar un tweet usando Selenium"""
        try:
            if not self.driver:
                self._init_driver()

            logger.info("[TWITTER] Navegando a X/Twitter...")
            self.driver.get("https://x.com/compose/post")

            # Esperar a que cargue el cuadro de texto
            wait = WebDriverWait(self.driver, 20)
            text_box = wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '[data-testid="tweetTextarea_0"]'))
            )

            time.sleep(2)

            # Hacer click en el cuadro de texto para activarlo
            text_box.click()
            time.sleep(1)

            # Insertar texto simulando un paste (send_keys no soporta emojis)
            self.driver.execute_script("""
                const editor = document.activeElement;
                const dt = new DataTransfer();
                dt.setData('text/plain', arguments[0]);
                const paste = new ClipboardEvent('paste', {
                    clipboardData: dt,
                    bubbles: true,
                    cancelable: true
                });
                editor.dispatchEvent(paste);
            """, text)

            time.sleep(1)

            # Clickear el botón de publicar
            post_button = wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, '[data-testid="tweetButton"]'))
            )
            post_button.click()

            logger.info("[TWITTER] Tweet publicado exitosamente")

            time.sleep(3)
            return True

        except Exception as e:
            logger.error(f"[TWITTER] Error publicando tweet: {e}")
            return False

    def close(self):
        """Cerrar el driver"""
        if self.driver:
            try:
                self.driver.quit()
                logger.info("[TWITTER] ChromeDriver cerrado")
            except Exception as e:
                logger.error(f"[TWITTER] Error cerrando driver: {e}")
            finally:
                self.driver = None


def test_twitter():
    """Test standalone de la integración de Twitter"""
    import sys

    logging.basicConfig(
        format='%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        level=logging.INFO
    )

    bot = TwitterBot()
    now = datetime.now(ARGENTINA_TZ).strftime("%Y-%m-%d %H:%M:%S")

    # --- Test 1: should_tweet con cambio >= 5pp ---
    print("\n" + "=" * 50)
    print("TEST 1: should_tweet con cambio >= 5pp")
    print("=" * 50)

    changes_big = {
        '1d': {'old': 35.0, 'new': 40.5, 'absolute': 5.5, 'percentage': 15.71, 'changed': True},
        '2d': {'old': 36.0, 'new': 36.2, 'absolute': 0.2, 'percentage': 0.56, 'changed': True},
        '3d': {'old': 36.5, 'new': 36.8, 'absolute': 0.3, 'percentage': 0.82, 'changed': True},
        '7d': {'old': 37.0, 'new': 37.5, 'absolute': 0.5, 'percentage': 1.35, 'changed': True},
    }

    result = bot.should_tweet(changes_big)
    print(f"  Cambio 1D: +5.5pp -> should_tweet = {result}")
    assert result is True, "ERROR: debería retornar True con cambio de 5.5pp"
    print("  ✅ PASSED")

    # --- Test 2: should_tweet con cambio < 5pp ---
    print("\n" + "=" * 50)
    print("TEST 2: should_tweet con cambio < 5pp")
    print("=" * 50)

    changes_small = {
        '1d': {'old': 35.0, 'new': 37.0, 'absolute': 2.0, 'percentage': 5.71, 'changed': True},
        '2d': {'old': 36.0, 'new': 36.2, 'absolute': 0.2, 'percentage': 0.56, 'changed': True},
        '3d': {'old': 36.5, 'new': 36.8, 'absolute': 0.3, 'percentage': 0.82, 'changed': True},
        '7d': {'old': 37.0, 'new': 37.5, 'absolute': 0.5, 'percentage': 1.35, 'changed': True},
    }

    result = bot.should_tweet(changes_small)
    print(f"  Cambio máximo: +2.0pp -> should_tweet = {result}")
    assert result is False, "ERROR: debería retornar False con cambio de 2.0pp"
    print("  ✅ PASSED")

    # --- Test 3: should_tweet con baja >= 5pp (no debe twittear) ---
    print("\n" + "=" * 50)
    print("TEST 3: should_tweet con BAJA >= 5pp (no twittea)")
    print("=" * 50)

    changes_down = {
        '1d': {'old': 40.0, 'new': 34.0, 'absolute': -6.0, 'percentage': -15.0, 'changed': True},
        '2d': {'old': 36.0, 'new': 36.0, 'absolute': 0.0, 'percentage': 0.0, 'changed': False},
        '3d': {'old': 36.5, 'new': 36.5, 'absolute': 0.0, 'percentage': 0.0, 'changed': False},
        '7d': {'old': 37.0, 'new': 37.0, 'absolute': 0.0, 'percentage': 0.0, 'changed': False},
    }

    result = bot.should_tweet(changes_down)
    print(f"  Cambio 1D: -6.0pp -> should_tweet = {result}")
    assert result is False, "ERROR: debería retornar False con baja"
    print("  ✅ PASSED")

    # --- Test 4: format_tweet ---
    print("\n" + "=" * 50)
    print("TEST 4: format_tweet")
    print("=" * 50)

    rates = {
        '1d': 40.50, '2d': 36.20, '3d': 36.80, '7d': 37.50,
        'timestamp': now
    }

    tweet_text = bot.format_tweet(rates, changes_big)
    print(f"\n{tweet_text}")
    print(f"\n  Caracteres: {len(tweet_text)}/280")
    assert len(tweet_text) <= 280, f"ERROR: tweet excede 280 chars ({len(tweet_text)})"
    assert "🔔" in tweet_text
    assert "📈" in tweet_text
    assert "40.50%" in tweet_text
    print("  ✅ PASSED")

    # --- Test 5: should_tweet sin cambios ---
    print("\n" + "=" * 50)
    print("TEST 5: should_tweet sin cambios (None)")
    print("=" * 50)

    result = bot.should_tweet(None)
    print(f"  changes=None -> should_tweet = {result}")
    assert result is False, "ERROR: debería retornar False con None"
    print("  ✅ PASSED")

    print("\n" + "=" * 50)
    print("✅ TODOS LOS TESTS PASARON")
    print("=" * 50)

    # --- Test real con --post ---
    if "--post" in sys.argv:
        print("\n" + "=" * 50)
        print("TEST REAL: Publicando tweet de prueba")
        print("=" * 50)

        test_tweet = bot.format_tweet(rates, changes_big)
        print(f"\nTweet a publicar:\n{test_tweet}\n")

        success = bot.tweet(test_tweet)
        if success:
            print("✅ Tweet publicado exitosamente")
        else:
            print("❌ Error publicando tweet")

        bot.close()


def calculate_changes(old_rates: dict, new_rates: dict) -> dict:
    """Misma lógica que CaucionBot.calculate_changes en main.py"""
    changes = {}
    for period in ['1d', '2d', '3d', '7d']:
        old_value = old_rates.get(period, 0)
        new_value = new_rates.get(period, 0)

        if old_value == 0:
            changes[period] = {'absolute': 0, 'percentage': 0, 'changed': False}
        else:
            absolute_change = new_value - old_value
            percentage_change = (absolute_change / old_value) * 100
            changes[period] = {
                'old': old_value,
                'new': new_value,
                'absolute': absolute_change,
                'percentage': percentage_change,
                'changed': abs(absolute_change) > 0.001
            }

    return changes


def simulate_flow():
    """Simula el flujo completo de check_rates_and_notify para Twitter"""
    import sys

    logging.basicConfig(
        format='%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        level=logging.INFO
    )

    bot = TwitterBot()
    now = datetime.now(ARGENTINA_TZ).strftime("%Y-%m-%d %H:%M:%S")

    # Simular tasas anteriores (lo que tendría self.last_rates)
    old_rates = {
        '1d': 18.00, '2d': 20.00, '3d': 22.00, '7d': 24.00,
        'timestamp': '2026-02-16 10:30:00'
    }

    # Simular tasas nuevas con suba >= 5pp en 1D
    new_rates = {
        '1d': 25.00, '2d': 21.00, '3d': 22.50, '7d': 24.50,
        'timestamp': now
    }

    print("\n" + "=" * 50)
    print("SIMULACIÓN: Flujo de check_rates_and_notify")
    print("=" * 50)

    print(f"\n  Tasas anteriores: 1D={old_rates['1d']}% | 2D={old_rates['2d']}% | 3D={old_rates['3d']}% | 7D={old_rates['7d']}%")
    print(f"  Tasas nuevas:     1D={new_rates['1d']}% | 2D={new_rates['2d']}% | 3D={new_rates['3d']}% | 7D={new_rates['7d']}%")

    # Paso 1: Calcular cambios (como hace main.py)
    changes = calculate_changes(old_rates, new_rates)

    print("\n  Cambios calculados:")
    for period in ['1d', '2d', '3d', '7d']:
        c = changes[period]
        if c['changed']:
            sign = '+' if c['absolute'] > 0 else ''
            print(f"    {period.upper()}: {sign}{c['absolute']:.2f}pp ({sign}{c['percentage']:.2f}%)")

    # Paso 2: Evaluar should_tweet
    should = bot.should_tweet(changes)
    print(f"\n  should_tweet() = {should}")

    if should:
        print("  → El bot SÍ publicaría en Twitter")

        # Paso 3: Formatear tweet
        tweet_text = bot.format_tweet(new_rates, changes)
        print(f"\n  Tweet ({len(tweet_text)}/280 chars):")
        print("  " + "-" * 40)
        for line in tweet_text.split('\n'):
            print(f"  {line}")
        print("  " + "-" * 40)

        # Paso 4: Publicar si se pasó --post
        if "--post" in sys.argv:
            print("\n  Publicando tweet real...")
            success = bot.tweet(tweet_text)
            if success:
                print("  ✅ Tweet publicado exitosamente")
            else:
                print("  ❌ Error publicando tweet")
            bot.close()
        else:
            print("\n  (usar --post para publicar de verdad)")
    else:
        print("  → El bot NO publicaría en Twitter")


if __name__ == "__main__":
    import sys

    if "--simulate" in sys.argv:
        simulate_flow()
    else:
        test_twitter()
