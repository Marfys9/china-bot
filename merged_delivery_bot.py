import os
import asyncio
import logging
import json
from decimal import Decimal
import aiohttp
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
from typing import Dict, Optional, Union
from bs4 import BeautifulSoup
import ssl
import xml.etree.ElementTree as ET


# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def parse_decimal(text: str) -> Decimal:
    """Преобразование строки в Decimal с учетом запятых"""
    try:
        # Заменяем запятую на точку и пробуем преобразовать
        return Decimal(text.replace(',', '.'))
    except:
        raise ValueError("Неверный формат числа")

class DeliveryStates(StatesGroup):
    """Состояния для конечного автомата бота"""
    # Состояния начальной настройки
    setup_cny_rate = State()
    setup_weight_tariff = State()
    setup_volume_coefficient = State()
    setup_photo_report = State()
    setup_duty_free_threshold = State()
    manual_eur_rate = State()
    
    # Состояния для расчета
    calc_length = State()
    calc_width = State()
    calc_height = State()
    calc_weight = State()
    calc_price_cny = State()
    calc_delivery_ru = State()

class SettingsManager:
    """Менеджер настроек пользователя"""
    
    @staticmethod
    def save_settings(user_id: int, settings: Dict[str, Decimal]) -> None:
        """Сохранение настроек пользователя"""
        # Изменяем путь для сохранения файлов
        settings_file = f'/app/data/user_settings_{user_id}.json'
        os.makedirs('/app/data', exist_ok=True)  # Создаём директорию, если её нет
        with open(settings_file, 'w') as f:
            json.dump({k: str(v) for k, v in settings.items()}, f)
    
    @staticmethod
    def load_settings(user_id: int) -> Optional[Dict[str, Decimal]]:
        """Загрузка настроек пользователя"""
        settings_file = f'/app/data/user_settings_{user_id}.json'
        try:
            with open(settings_file, 'r') as f:
                settings = json.load(f)
                return {k: Decimal(v) for k, v in settings.items()}
        except (FileNotFoundError, json.JSONDecodeError):
            return None

class DeliveryBot:
    """Основной класс бота для расчета доставки"""
    
    def __init__(self, token: str):
        """Инициализация бота"""
        self.bot = Bot(token=token)
        self.dp = Dispatcher(storage=MemoryStorage())
        self.last_eur_rate: Optional[Decimal] = None
        self.register_handlers()

    async def fetch_eur_rate(self) -> Optional[Decimal]:
        """Получение курса евро с использованием нескольких методов"""
        # Создаем контекст SSL с отключенной проверкой сертификата
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        # 1. Пробуем получить курс от ЦБ РФ
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get('https://www.cbr.ru/scripts/XML_daily.asp', ssl=ssl_context) as response:
                    if response.status == 200:
                        data = await response.text()
                        root = ET.fromstring(data)
                        for valute in root.findall('.//Valute'):
                            if valute.find('CharCode').text == 'EUR':
                                rate_str = valute.find('Value').text.replace(',', '.')
                                rate = Decimal(rate_str)
                                self.last_eur_rate = rate
                                logger.info(f"Получен курс евро ЦБ РФ: {rate}")
                                return rate
        except Exception as e:
            logger.error(f"Ошибка при получении курса через ЦБ РФ: {e}")

        # 2. Если ЦБ РФ не сработал, пробуем альтернативный API ЦБ РФ
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get('https://www.cbr-xml-daily.ru/daily_json.js', ssl=ssl_context) as response:
                    if response.status == 200:
                        text = await response.text()
                        data = json.loads(text)
                        rate = Decimal(str(data['Valute']['EUR']['Value']))
                        self.last_eur_rate = rate
                        logger.info(f"Получен курс евро через альтернативный API ЦБ РФ: {rate}")
                        return rate
        except Exception as e:
            logger.error(f"Ошибка при получении курса через альтернативный API ЦБ РФ: {e}")

        # 3. Если оба метода ЦБ РФ не сработали, пробуем Google Finance
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get('https://www.google.com/finance/quote/EUR-RUB', ssl=ssl_context) as response:
                    if response.status == 200:
                        data = await response.text()
                        soup = BeautifulSoup(data, 'html.parser')
                        rate_element = soup.select_one('div[data-last-price]')
                        if rate_element:
                            rate = Decimal(rate_element['data-last-price'])
                            self.last_eur_rate = rate
                            logger.info(f"Получен курс евро Google Finance: {rate}")
                            return rate
        except Exception as e:
            logger.error(f"Ошибка при получении курса через Google Finance: {e}")

        # Если есть сохраненный курс, используем его
        if self.last_eur_rate:
            logger.warning(f"Используется последний известный курс евро: {self.last_eur_rate}")
            return self.last_eur_rate

        # Если все методы не сработали и нет сохраненного курса, возвращаем None
        logger.error("Не удалось получить курс евро ни одним из методов")
        return None

    def get_main_keyboard(self) -> ReplyKeyboardMarkup:
        """Создание основной клавиатуры"""
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📊 Произвести расчет")],
                [KeyboardButton(text="⚙️ Изменить настройки")],
                [KeyboardButton(text="🔄 Перезапустить бот")]
            ],
            resize_keyboard=True
        )


    def register_handlers(self) -> None:
        """Регистрация всех обработчиков бота"""
        
        @self.dp.message(Command('start'))
        async def start_command(message: Message, state: FSMContext):
            """Обработчик команды /start"""
            user_settings = SettingsManager.load_settings(message.from_user.id)
            if user_settings:
                await message.answer(
                    "Добро пожаловать! Выберите действие:",
                    reply_markup=self.get_main_keyboard()
                )
            else:
                await message.answer("Добро пожаловать! Давайте настроим бот.")
                await state.set_state(DeliveryStates.setup_cny_rate)
                await message.answer("Введите курс CNY (можно использовать запятую, например: 12,5):")

        @self.dp.message(Command('restart'))
        async def restart_command(message: Message, state: FSMContext):
            """Обработчик команды перезапуска"""
            await state.clear()
            await start_command(message, state)

        @self.dp.message(F.text == "🔄 Перезапустить бот")
        async def restart_button(message: Message, state: FSMContext):
            """Обработчик кнопки перезапуска"""
            await restart_command(message, state)

        @self.dp.message(F.text == "⚙️ Изменить настройки")
        async def settings_menu(message: Message, state: FSMContext):
            """Обработчик меню настроек"""
            await message.answer(
                "Давайте обновим настройки бота.",
                reply_markup=ReplyKeyboardRemove()
            )
            await state.set_state(DeliveryStates.setup_cny_rate)
            await message.answer("Введите курс CNY (можно использовать запятую, например: 12,5):")

        @self.dp.message(DeliveryStates.setup_cny_rate)
        async def process_cny_rate(message: Message, state: FSMContext):
            """Обработчик установки курса CNY"""
            try:
                rate = parse_decimal(message.text)
                if rate <= 0:
                    raise ValueError("Курс должен быть положительным числом")
                
                await state.update_data(cny_rate=rate)
                await state.set_state(DeliveryStates.setup_weight_tariff)
                await message.answer("Введите тариф по весу (руб/кг):")
            except ValueError:
                await message.answer("Пожалуйста, введите корректное положительное число (например: 12,5)")

        @self.dp.message(DeliveryStates.setup_weight_tariff)
        async def process_weight_tariff(message: Message, state: FSMContext):
            """Обработчик установки тарифа по весу"""
            try:
                tariff = parse_decimal(message.text)
                if tariff <= 0:
                    raise ValueError("Тариф должен быть положительным числом")
                
                await state.update_data(weight_tariff=tariff)
                await state.set_state(DeliveryStates.setup_volume_coefficient)
                await message.answer("Введите тариф за доп. коэффициент (руб/кг):")
            except ValueError:
                await message.answer("Пожалуйста, введите корректное положительное число (например: 12,5)")

        @self.dp.message(DeliveryStates.setup_volume_coefficient)
        async def process_volume_coefficient(message: Message, state: FSMContext):
            """Обработчик установки объемного коэффициента"""
            try:
                coefficient = parse_decimal(message.text)
                if coefficient <= 0:
                    raise ValueError("Коэффициент должен быть положительным числом")
                
                await state.update_data(volume_coefficient=coefficient)
                await state.set_state(DeliveryStates.setup_photo_report)
                await message.answer("Введите стоимость фотоотчета:")
            except ValueError:
                await message.answer("Пожалуйста, введите корректное положительное число (например: 12,5)")

        @self.dp.message(DeliveryStates.setup_photo_report)
        async def process_photo_report(message: Message, state: FSMContext):
            """Обработчик установки стоимости фотоотчета"""
            try:
                cost = parse_decimal(message.text)
                if cost < 0:
                    raise ValueError("Стоимость не может быть отрицательной")
                
                await state.update_data(photo_report=cost)
                await state.set_state(DeliveryStates.setup_duty_free_threshold)
                await message.answer("Введите беспошлинный порог (EUR):")
            except ValueError:
                await message.answer("Пожалуйста, введите корректное неотрицательное число (например: 12,5)")

        @self.dp.message(DeliveryStates.setup_duty_free_threshold)
        async def process_duty_free_threshold(message: Message, state: FSMContext):
            """Обработчик установки беспошлинного порога"""
            try:
                threshold = parse_decimal(message.text)
                if threshold <= 0:
                    raise ValueError("Порог должен быть положительным числом")
                
                data = await state.get_data()
                settings = {
                    'cny_rate': data['cny_rate'],
                    'weight_tariff': data['weight_tariff'],
                    'volume_coefficient': data['volume_coefficient'],
                    'photo_report': data['photo_report'],
                    'duty_free_threshold': threshold
                }
                SettingsManager.save_settings(message.from_user.id, settings)
                
                settings_info = f"""
⚙️ Текущие настройки:
└ 💴 Курс CNY: {settings['cny_rate']}
└ ⚖️ Тариф по весу: {settings['weight_tariff']} руб/кг
└ 📏 Объемный коэффициент: {settings['volume_coefficient']} руб/кг
└ 📸 Стоимость фотоотчета: {settings['photo_report']} руб
└ 🏛️ Беспошлинный порог: {settings['duty_free_threshold']} EUR
"""
                await message.answer(settings_info)
                await message.answer(
                    "Настройки успешно обновлены! Выберите действие:",
                    reply_markup=self.get_main_keyboard()
                )
                await state.clear()
            except ValueError:
                await message.answer("Пожалуйста, введите корректное положительное число (например: 12,5)")


        @self.dp.message(F.text == "📊 Произвести расчет")
        async def start_calculation(message: Message, state: FSMContext):
            """Начало процесса расчета"""
            await state.set_state(DeliveryStates.calc_length)
            await message.answer(
                "Введите длину (см):",
                reply_markup=ReplyKeyboardRemove()
            )

        @self.dp.message(DeliveryStates.calc_length)
        async def process_length(message: Message, state: FSMContext):
            """Обработчик ввода длины"""
            try:
                length = parse_decimal(message.text)
                if length <= 0:
                    raise ValueError("Длина должна быть положительным числом")
                
                await state.update_data(length=length)
                await state.set_state(DeliveryStates.calc_width)
                await message.answer("Введите ширину (см):")
            except ValueError:
                await message.answer("Пожалуйста, введите корректное положительное число (например: 12,5)")

        @self.dp.message(DeliveryStates.calc_width)
        async def process_width(message: Message, state: FSMContext):
            """Обработчик ввода ширины"""
            try:
                width = parse_decimal(message.text)
                if width <= 0:
                    raise ValueError("Ширина должна быть положительным числом")
                
                await state.update_data(width=width)
                await state.set_state(DeliveryStates.calc_height)
                await message.answer("Введите высоту (см):")
            except ValueError:
                await message.answer("Пожалуйста, введите корректное положительное число (например: 12,5)")

        @self.dp.message(DeliveryStates.calc_height)
        async def process_height(message: Message, state: FSMContext):
            """Обработчик ввода высоты"""
            try:
                height = parse_decimal(message.text)
                if height <= 0:
                    raise ValueError("Высота должна быть положительным числом")
                
                await state.update_data(height=height)
                await state.set_state(DeliveryStates.calc_weight)
                await message.answer("Введите вес (кг):")
            except ValueError:
                await message.answer("Пожалуйста, введите корректное положительное число (например: 12,5)")

        @self.dp.message(DeliveryStates.calc_weight)
        async def process_weight(message: Message, state: FSMContext):
            """Обработчик ввода веса"""
            try:
                weight = parse_decimal(message.text)
                if weight <= 0:
                    raise ValueError("Вес должен быть положительным числом")
                
                await state.update_data(weight=weight)
                await state.set_state(DeliveryStates.calc_price_cny)
                await message.answer("Введите стоимость товара (CNY):")
            except ValueError:
                await message.answer("Пожалуйста, введите корректное положительное число (например: 12,5)")

        @self.dp.message(DeliveryStates.calc_price_cny)
        async def process_price_cny(message: Message, state: FSMContext):
            """Обработчик ввода стоимости в юанях"""
            try:
                price = parse_decimal(message.text)
                if price <= 0:
                    raise ValueError("Стоимость должна быть положительным числом")
                
                await state.update_data(price_cny=price)
                await state.set_state(DeliveryStates.calc_delivery_ru)
                await message.answer("Введите стоимость доставки по РФ (руб):")
            except ValueError:
                await message.answer("Пожалуйста, введите корректное положительное число (например: 12,5)")

        @self.dp.message(DeliveryStates.calc_delivery_ru)
        async def process_delivery_ru(message: Message, state: FSMContext):
            """Обработчик ввода стоимости доставки по РФ"""
            try:
                delivery_ru = parse_decimal(message.text)
                if delivery_ru < 0:
                    raise ValueError("Стоимость доставки не может быть отрицательной")
                
                await state.update_data(delivery_ru=delivery_ru)
                # Переходим к финальному расчету
                await process_calculation(message, state)
            except ValueError:
                await message.answer("Пожалуйста, введите корректное неотрицательное число (например: 12,5)")

        async def process_calculation(message: Message, state: FSMContext):
            """Финальный расчет стоимости"""
            try:
                data = await state.get_data()
                settings = SettingsManager.load_settings(message.from_user.id)
                
                if not settings:
                    await message.answer("Настройки не найдены. Пожалуйста, настройте бот заново.")
                    return

                # Получение курса евро
                eur_rate = await self.fetch_eur_rate()
                if not eur_rate:
                    await message.answer(
                        "Не удалось автоматически получить курс евро. Пожалуйста, введите текущий курс евро вручную:"
                    )
                    await state.set_state(DeliveryStates.manual_eur_rate)
                    return

                # Расчет объема
                volume = (data['length'] / 100) * (data['width'] / 100) * (data['height'] / 100)  # в м³
                volume_weight = volume * Decimal('167')
                
                # Расчет доп. коэффициента за объем
                volume_coefficient = Decimal('0')
                if volume_weight > data['weight']:
                    volume_coefficient = (volume_weight - data['weight']) * settings['volume_coefficient']

                # Расчет полной стоимости доставки ДП
                delivery_cost = (data['weight'] * settings['weight_tariff']) + volume_coefficient

                # Расчет стоимости в евро и пошлины
                price_eur = (data['price_cny'] * settings['cny_rate']) / eur_rate
                
                # Расчет пошлины
                duty = Decimal('0')
                if price_eur > settings['duty_free_threshold']:
                    duty_excess = price_eur - settings['duty_free_threshold']
                    base_duty = (duty_excess * Decimal('0.15')) * eur_rate + Decimal('500')
                    duty = base_duty * Decimal('1.05')

                # Расчет себестоимости с доставкой до РФ
                total_cost = (
                    (data['price_cny'] * settings['cny_rate']) +  # Стоимость в рублях
                    delivery_cost +  # Стоимость доставки ДП
                    data['delivery_ru'] +  # Доставка по РФ
                    duty +  # Пошлина
                    settings['photo_report']  # Стоимость фотоотчета
                )

                # Расчет стоимости с наценками
                cost_20_percent = total_cost * Decimal('1.2')
                cost_30_percent = total_cost * Decimal('1.3')

                # Форматирование результата
                result = f"""
⚙️ Текущие настройки:
└ 💴 Курс CNY: {settings['cny_rate']}
└ ⚖️ Тариф по весу: {settings['weight_tariff']} руб/кг
└ 📏 Объемный коэффициент: {settings['volume_coefficient']} руб/кг
└ 📸 Стоимость фотоотчета: {settings['photo_report']} руб
└ 🏛️ Беспошлинный порог: {settings['duty_free_threshold']} EUR

📦 Параметры посылки:
└ 💵 Цена в юанях: {data['price_cny']} CNY
└ 📐 Габариты (В×Ш×Д): {data['height']}×{data['width']}×{data['length']} см
└ ⚖️ Фактический вес: {data['weight']} кг
└ 📊 Объемный вес: {volume_weight:.2f} кг

💰 Расчет стоимости:
└ 🏛️ При курсе евро {eur_rate} - пошлина будет {duty:.2f} руб
└ 📈 Доп. коэффициент за объем: {volume_coefficient:.2f} руб
└ 🚚 Полная стоимость доставки ДП: {delivery_cost:.2f} руб
└ 💎 Себестоимость с дост до РФ: {total_cost:.2f} руб
└ 💹 Стоимость с наценкой 20%: {cost_20_percent:.2f} руб
└ 💰 Стоимость с наценкой 30%: {cost_30_percent:.2f} руб
└ 🚛 Доставка по РФ: {data['delivery_ru']} руб
"""
                await message.answer(result, reply_markup=self.get_main_keyboard())
                await state.clear()

            except Exception as e:
                logger.error(f"Ошибка при расчете: {e}")
                await message.answer(
                    "Произошла ошибка при расчете. Пожалуйста, попробуйте снова.",
                    reply_markup=self.get_main_keyboard()
                )
                await state.clear()

        @self.dp.message(DeliveryStates.manual_eur_rate)
        async def process_manual_eur_rate(message: Message, state: FSMContext):
            """Обработчик ввода курса евро вручную"""
            try:
                eur_rate = parse_decimal(message.text)
                if eur_rate <= 0:
                    raise ValueError("Курс евро должен быть положительным числом")
                
                self.last_eur_rate = eur_rate
                await process_calculation(message, state)
            except ValueError:
                await message.answer("Пожалуйста, введите корректное положительное число (например: 12,5)")

async def main():
    """Основная функция запуска бота"""
    load_dotenv()
    bot = DeliveryBot(os.getenv('BOT_TOKEN'))
    try:
        await bot.dp.start_polling(bot.bot)
    finally:
        await bot.bot.session.close()

if __name__ == '__main__':
    asyncio.run(main())