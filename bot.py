
# Импорт необходимых библиотек
import asyncio
import logging
import time
from typing import Optional, Union, List, Dict, Any, Tuple
from dataclasses import dataclass
from enum import Enum
import re

import gspread
import telegram
from oauth2client.service_account import ServiceAccountCredentials
from gspread import Client
from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

# Перечисления для типов контента
class ContentType(Enum):
    TEXT = "text"
    PHONE = "phone" 
    MENU = "menu"
    SUBMENU = "submenu"
    LINK = "link"
    EMAIL = "email"

# Константы для индексов столбцов таблицы Google Sheets
class SheetColumns:
    CALLBACK_DATA = 0  # Уникальный идентификатор для callback_data
    PARENT = 1         # Родительский элемент в иерархии меню
    TEXT = 2           # Текст для отображения пользователю
    DATA = 3           # Дополнительные данные (телефоны, ссылки и т.д.)
    TYPE = 4           # Тип контента (menu, submenu, text, link и т.д.)
    EXTRA = 5          # Дополнительная информация

@dataclass
class MenuItem:
    """Класс для представления элемента меню"""
    callback_data: str
    parent: str
    text: str
    data: str = ""
    content_type: ContentType = ContentType.TEXT
    extra: str = ""
    
    @classmethod
    def from_row(cls, row: List[str]) -> Optional['MenuItem']:
        """Создает объект MenuItem из строки таблицы"""
        if len(row) < 3:
            return None
            
        callback_data = row[0].strip()
        parent = row[1].strip()
        text = row[2].strip()
        
        if not callback_data or not text:
            return None
            
        data = row[3].strip() if len(row) > 3 else ""
        content_type_str = row[4].strip() if len(row) > 4 else "text"
        extra = row[5].strip() if len(row) > 5 else ""
        
        try:
            content_type = ContentType(content_type_str)
        except ValueError:
            content_type = ContentType.TEXT
            
        return cls(
            callback_data=callback_data,
            parent=parent,
            text=text,
            data=data,
            content_type=content_type,
            extra=extra
        )

# Конфигурация
class Config:
    TOKEN = " " 
    CREDS_FILE = " "
    SHEET_ID = " "
    CACHE_DURATION = 3600  # 1 час
    MAX_SEARCH_RESULTS = 5
    MAX_MESSAGE_LENGTH = 4000
    
    # Кнопки интерфейса
    CONTACTS_BUTTON = "📞 Важные контакты"
    MENU_BUTTON = "📋 Меню"
    HELP_BUTTON = "❓ Помощь"
    REFRESH_BUTTON = "🔄 Обновить"

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class DataCache:
    """Кэш для данных Google Sheets"""
    
    def __init__(self, cache_duration: int = Config.CACHE_DURATION):
        self.cache_duration = cache_duration
        self.last_update: Optional[float] = None
        self.data: List[MenuItem] = []
        
    def is_valid(self) -> bool:
        """Проверяет, актуален ли кэш"""
        return (self.last_update is not None and 
                time.time() - self.last_update < self.cache_duration)
    
    def update(self, data: List[MenuItem]) -> None:
        """Обновляет кэш"""
        self.data = data
        self.last_update = time.time()
        
    def clear(self) -> None:
        """Очищает кэш"""
        self.data = []
        self.last_update = None

class GoogleSheetsManager:
    """Менеджер для работы с Google Sheets"""
    
    def __init__(self, creds_file: str, sheet_id: str):
        """
        Инициализация менеджера
        
        Args:
            creds_file: Путь к файлу учетных данных Google Service Account
            sheet_id: ID Google-таблицы
        """
        if not creds_file:
            raise ValueError("Необходимо указать creds_file")
        if not sheet_id:
            raise ValueError("Необходимо указать sheet_id")
            
        self.creds_file = creds_file
        self.sheet_id = sheet_id
        self._client = None
        
    async def _get_client(self) -> Client:
        """Получает клиент Google Sheets"""
        if self._client is None:
            try:
                scope = [
                    "https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive"
                ]
                creds = ServiceAccountCredentials.from_json_keyfile_name(
                    self.creds_file, scope
                )
                self._client = gspread.authorize(creds)
            except Exception as e:
                logger.error(f"Ошибка создания клиента Google Sheets: {e}")
                raise
        return self._client
        
    async def fetch_data(self) -> List[MenuItem]:
        """Получает данные из Google Sheets"""
        try:
            client = await self._get_client()
            sheet = client.open_by_key(self.sheet_id).sheet1
            raw_data = sheet.get_all_values()
            
            menu_items = []
            for row in raw_data[1:]:  # Пропускаем заголовок
                if not row or not any(cell.strip() for cell in row):
                    continue
                    
                item = MenuItem.from_row(row)
                if item:
                    menu_items.append(item)
                    
            logger.info(f"Успешно загружено {len(menu_items)} элементов меню")
            return menu_items
            
        except gspread.SpreadsheetNotFound:
            logger.error(f"Таблица с ID {self.sheet_id} не найдена")
            raise
        except gspread.APIError as e:
            logger.error(f"Ошибка Google API: {e}")
            raise
        except Exception as e:
            logger.error(f"Неожиданная ошибка при получении данных: {e}")
            raise

    async def update_data(self, data: List[List[str]]) -> None:
        """Обновляет данные в таблице"""
        try:
            client = await self._get_client()
            sheet = client.open_by_key(self.sheet_id).sheet1
            sheet.clear()
            sheet.append_rows(data)
            logger.info("Данные успешно обновлены")
        except Exception as e:
            logger.error(f"Ошибка при обновлении данных: {e}")
            raise

class MessageFormatter:
    """Форматирование сообщений"""
    
    @staticmethod
    def format_phone(text: str, phone: str, extra: str = "") -> str:
        """Форматирует сообщение с телефоном"""
        formatted_phone = re.sub(r'\D', '', phone)
        if formatted_phone.startswith('8'):
            formatted_phone = '7' + formatted_phone[1:]
        
        message = f"{text}\n\n📞 <a href='tel:+{formatted_phone}'>{phone}</a>"
        
        if extra:
            message += f"\n\n💡 {extra}"
            
        return message
    
    @staticmethod
    def format_text(text: str, data: str = "", extra: str = "") -> str:
        """Форматирует обычное текстовое сообщение"""
        message = text
        
        if data:
            message += f"\n\n{data}"
            
        if extra:
            message += f"\n\n💡 {extra}"
            
        return message
    
    @staticmethod
    def format_link(text: str, url: str, extra: str = "") -> str:
        """Форматирует сообщение со ссылкой"""
        message = f"{text}\n\n🔗 <a href='{url}'>Перейти по ссылке</a>"
        
        if extra:
            message += f"\n\n💡 {extra}"
            
        return message
    
    @staticmethod
    def truncate_message(message: str, max_length: int = Config.MAX_MESSAGE_LENGTH) -> str:
        """Обрезает сообщение до максимальной длины"""
        if len(message) <= max_length:
            return message
        return message[:max_length - 3] + "..."

class NavigationManager:
    """Менеджер навигации"""
    
    @staticmethod
    def get_nav_stack(context: CallbackContext) -> List[str]:
        """Получает стек навигации"""
        return context.user_data.get('nav_stack', [])
    
    @staticmethod
    def push_to_nav_stack(context: CallbackContext, item_id: str) -> None:
        """Добавляет элемент в стек навигации"""
        if 'nav_stack' not in context.user_data:
            context.user_data['nav_stack'] = []
        context.user_data['nav_stack'].append(item_id)
    
    @staticmethod
    def pop_from_nav_stack(context: CallbackContext) -> Optional[str]:
        """Удаляет и возвращает последний элемент из стека"""
        nav_stack = NavigationManager.get_nav_stack(context)
        return nav_stack.pop() if nav_stack else None
    
    @staticmethod
    def clear_nav_stack(context: CallbackContext) -> None:
        """Очищает стек навигации"""
        context.user_data['nav_stack'] = []

class ClinicBot:
    """Основной класс бота клиники"""
    
    def __init__(self, token: str, creds_file: str, sheet_id: str):
        self.token = token
        self.application = Application.builder().token(token).build()
        self.cache = DataCache()
        self.sheets_manager = GoogleSheetsManager(creds_file, sheet_id)
        self.formatter = MessageFormatter()
        self.nav_manager = NavigationManager()
        
        # Регистрируем обработчики
        self._register_handlers()
        
    def _register_handlers(self) -> None:
        """Регистрирует обработчики команд и сообщений"""
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("menu", self.menu_command))
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_message)
        )
        self.application.add_handler(CallbackQueryHandler(self.handle_callback_query))
        
    async def get_menu_data(self, force_refresh: bool = False) -> List[MenuItem]:
        """Получает данные меню с кэшированием"""
        if force_refresh or not self.cache.is_valid():
            logger.info("Обновляем данные из Google Sheets...")
            data = await self.sheets_manager.fetch_data()
            self.cache.update(data)
        
        return self.cache.data
    
    async def start_command(self, update: Update, context: CallbackContext) -> None:
        """Обработчик команды /start"""
        if not update.message or not update.effective_user:
            return
            
        user = update.effective_user
        logger.info(f"Пользователь {user.id} (@{user.username}) запустил бота")
        
        welcome_text = (
            f"👋 Добро пожаловать, {user.first_name}!\n\n"
            "🏥 Это бот базы знаний клиники.\n\n"
            "Возможности:\n"
            "• 🔍 Поиск информации по ключевым словам\n"
            "• 📱 Быстрый доступ к контактам\n"
            "• 📋 Навигация по разделам\n\n"
            "Используйте кнопки ниже или введите запрос:"
        )
        
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [Config.MENU_BUTTON, Config.CONTACTS_BUTTON],
                ["💻 ЕМИАС", "🏥 Больничный"],
                [ Config.HELP_BUTTON]
            ],
            resize_keyboard=True,
            input_field_placeholder="Введите запрос или выберите кнопку"
        )
        
        await self._safe_send_message(
            chat_id=update.message.chat_id,
            text=welcome_text,
            reply_markup=keyboard
        )
    
    async def help_command(self, update: Update, context: CallbackContext) -> None:
        """Обработчик команды /help"""
        if not update.message:
            return
            
        help_text = (
            "❓ <b>Справка по использованию бота</b>\n\n"
            "🔍 <b>Поиск:</b>\n"
            "Просто напишите ключевое слово или фразу\n\n"
            "📋 <b>Навигация:</b>\n"
            "• Используйте кнопки для перехода по разделам\n"
            "• Кнопка 'Назад' для возврата\n"
            "• Команда /menu для главного меню\n\n"
            "📞 <b>Контакты:</b>\n"
            "Нажмите на номер телефона для звонка\n\n"
            "🔄 <b>Обновление:</b>\n"
            "Данные обновляются автоматически каждый час"
        )
        
        await self._safe_send_message(
            chat_id=update.message.chat_id,
            text=help_text,
            parse_mode="HTML"
        )
    
    async def menu_command(self, update: Update, context: CallbackContext) -> None:
        """Обработчик команды /menu"""
        if not update.message:
            return
            
        self.nav_manager.clear_nav_stack(context)
        sent_message = await update.message.reply_text("📋 Загружаем главное меню...")
        await self._show_main_menu(sent_message)
    
    async def handle_text_message(self, update: Update, context: CallbackContext) -> None:
        """Обработчик текстовых сообщений"""
        if not update.message or not update.message.text:
            return
            
        text = update.message.text.strip()
        
        # Обработка кнопок клавиатуры
        if text == Config.MENU_BUTTON:
            await self.menu_command(update, context)
            return
        elif text == Config.CONTACTS_BUTTON:
            await self._show_contacts(update)
            return
        elif text == Config.HELP_BUTTON:
            await self.help_command(update, context)
            return
        
        # Поиск по содержимому
        await self._handle_search(update, text)
    
    async def handle_callback_query(self, update: Update, context: CallbackContext) -> None:
        """Обработчик callback-запросов"""
        query = update.callback_query
        if not query:
            return
            
        await query.answer()
        
        try:
            # Специальные команды
            if query.data == "back":
                await self._handle_back_button(query, context)
                return
            elif query.data == "refresh":
                await self._handle_refresh(query, context)
                return
            elif query.data == "main_menu":
                self.nav_manager.clear_nav_stack(context)
                await self._show_main_menu(query.message)
                return
            
            # Поиск элемента меню
            menu_data = await self.get_menu_data()
            item = self._find_menu_item(menu_data, query.data)
            
            if not item:
                await self._show_error(query.message, "Элемент не найден")
                return
            
            # Обновляем навигацию и показываем контент
            self.nav_manager.push_to_nav_stack(context, item.callback_data)
            await self._show_item_content(query.message, item, menu_data)
            
        except Exception as e:
            logger.error(f"Ошибка в callback handler: {e}", exc_info=True)
            await self._show_error(query.message, "Произошла ошибка")
    
    async def _show_item_content(self, message: Message, item: MenuItem, menu_data: List[MenuItem]) -> None:
        """Показывает содержимое элемента меню"""
        if item.content_type == ContentType.MENU:
            await self._show_menu(message, item, menu_data, is_main=True)
        elif item.content_type == ContentType.SUBMENU:
            await self._show_menu(message, item, menu_data, is_main=False)
        elif item.content_type == ContentType.PHONE:
            await self._show_phone_content(message, item)
        elif item.content_type == ContentType.LINK:
            await self._show_link_content(message, item)
        else:
            await self._show_text_content(message, item)
    
    async def _show_menu(self, message: Message, parent_item: MenuItem, menu_data: List[MenuItem], is_main: bool = False) -> None:
        """Показывает меню или подменю"""
        # Находим дочерние элементы
        children = [
            item for item in menu_data 
            if item.parent == parent_item.callback_data
        ]
        
        if not children:
            await self._show_text_content(message, parent_item)
            return
        
        # Создаем клавиатуру
        keyboard = []
        for child in children:
            keyboard.append([InlineKeyboardButton(
                child.text,
                callback_data=child.callback_data
            )])
        
        # Добавляем кнопки навигации
        nav_buttons = []
        if not is_main:
            nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data="back"))
        nav_buttons.append(InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu"))
        nav_buttons.append(InlineKeyboardButton("🔄 Обновить", callback_data="refresh"))
        
        if nav_buttons:
            keyboard.append(nav_buttons)
        
        text = parent_item.text
        if parent_item.data:
            text += f"\n\n{parent_item.data}"
        
        await self._safe_edit_message(
            message=message,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    async def _show_phone_content(self, message: Message, item: MenuItem) -> None:
        """Показывает контент с телефоном"""
        formatted_text = self.formatter.format_phone(item.text, item.data, item.extra)
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="back")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
        ])
        
        await self._safe_edit_message(
            message=message,
            text=formatted_text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    
    async def _show_link_content(self, message: Message, item: MenuItem) -> None:
        """Показывает контент со ссылкой"""
        formatted_text = self.formatter.format_link(item.text, item.data, item.extra)
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="back")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
        ])
        
        await self._safe_edit_message(
            message=message,
            text=formatted_text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    
    async def _show_text_content(self, message: Message, item: MenuItem) -> None:
        """Показывает текстовый контент"""
        formatted_text = self.formatter.format_text(item.text, item.data, item.extra)
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="back")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
        ])
        
        await self._safe_edit_message(
            message=message,
            text=formatted_text,
            reply_markup=keyboard
        )
    
    async def _show_main_menu(self, message: Message) -> None:
        """Показывает главное меню"""
        menu_data = await self.get_menu_data()
        main_items = [item for item in menu_data if not item.parent]
        
        if not main_items:
            await self._show_error(message, "Главное меню не настроено")
            return
        
        keyboard = []
        for item in main_items:
            keyboard.append([InlineKeyboardButton(
                item.text,
                callback_data=item.callback_data
            )])
        
        keyboard.append([InlineKeyboardButton("🔄 Обновить данные", callback_data="refresh")])
        
        await self._safe_edit_message(
            message=message,
            text="📋 <b>Главное меню</b>\n\nВыберите нужный раздел:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
    
    async def _show_contacts(self, update: Update) -> None:
        """Показывает контакты"""
        menu_data = await self.get_menu_data()
        contacts_item = self._find_menu_item(menu_data, "main_contacts")
        
        if contacts_item:
            sent_message = await update.message.reply_text("📞 Загружаем контакты...")
            await self._show_item_content(sent_message, contacts_item, menu_data)
        else:
            await update.message.reply_text("⚠️ Контакты временно недоступны")
    
    async def _handle_search(self, update: Update, search_text: str) -> None:
        """Обрабатывает поиск по тексту"""
        if not search_text or len(search_text) < 2:
            await update.message.reply_text("🔍 Введите минимум 2 символа для поиска")
            return
        
        menu_data = await self.get_menu_data()
        results = self._search_items(menu_data, search_text)
        
        if not results:
            await update.message.reply_text(f"🔍 По запросу '{search_text}' ничего не найдено")
            return
        
        # Формируем ответ
        response_text = f"🔍 <b>Результаты поиска по запросу:</b> {search_text}\n\n"
        
        for i, item in enumerate(results[:Config.MAX_SEARCH_RESULTS], 1):
            response_text += f"{i}. <b>{item.text}</b>\n"
            if item.data:
                preview = item.data[:100]
                if len(item.data) > 100:
                    preview += "..."
                response_text += f"   {preview}\n"
            response_text += "\n"
        
        if len(results) > Config.MAX_SEARCH_RESULTS:
            response_text += f"... и еще {len(results) - Config.MAX_SEARCH_RESULTS} результатов"
        
        # Создаем клавиатуру с результатами поиска
        keyboard = []
        for item in results[:Config.MAX_SEARCH_RESULTS]:
            keyboard.append([InlineKeyboardButton(
                f"📄 {item.text}",
                callback_data=item.callback_data
            )])
        
        await self._safe_send_message(
            chat_id=update.message.chat_id,
            text=self.formatter.truncate_message(response_text),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
    
    def _search_items(self, items: List[MenuItem], search_text: str) -> List[MenuItem]:
        """Ищет элементы по тексту"""
        search_lower = search_text.lower()
        results = []
        
        for item in items:
            # Поиск в тексте
            if search_lower in item.text.lower():
                results.append(item)
            # Поиск в данных
            elif search_lower in item.data.lower():
                results.append(item)
            # Поиск в дополнительной информации
            elif search_lower in item.extra.lower():
                results.append(item)
        
        return results
    
    def _find_menu_item(self, items: List[MenuItem], callback_data: str) -> Optional[MenuItem]:
        """Находит элемент меню по callback_data"""
        return next((item for item in items if item.callback_data == callback_data), None)
    
    async def _handle_back_button(self, query: CallbackQuery, context: CallbackContext) -> None:
        """Обрабатывает кнопку 'Назад'"""
        nav_stack = self.nav_manager.get_nav_stack(context)
        
        if len(nav_stack) < 2:
            await self._show_main_menu(query.message)
            return
        
        # Удаляем текущий элемент
        self.nav_manager.pop_from_nav_stack(context)
        # Получаем предыдущий элемент
        previous_id = nav_stack[-1] if nav_stack else None
        
        if previous_id:
            menu_data = await self.get_menu_data()
            previous_item = self._find_menu_item(menu_data, previous_id)
            
            if previous_item:
                await self._show_item_content(query.message, previous_item, menu_data)
            else:
                await self._show_main_menu(query.message)
        else:
            await self._show_main_menu(query.message)
    
    async def _handle_refresh(self, query: CallbackQuery, context: CallbackContext) -> None:
        """Обрабатывает обновление данных"""
        await query.edit_message_text("🔄 Обновляем данные...")
        
        # Очищаем кэш и загружаем свежие данные
        self.cache.clear()
        await self.get_menu_data(force_refresh=True)
        
        # Показываем главное меню
        self.nav_manager.clear_nav_stack(context)
        await self._show_main_menu(query.message)
    
    async def _show_error(self, message: Message, error_text: str) -> None:
        """Показывает сообщение об ошибке"""
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
            [InlineKeyboardButton("🔄 Обновить", callback_data="refresh")]
        ])
        
        await self._safe_edit_message(
            message=message,
            text=f"⚠️ {error_text}",
            reply_markup=keyboard
        )
    
    async def _safe_send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup=None,
        parse_mode=None,
        disable_web_page_preview=True
    ) -> Optional[Message]:
        """Безопасная отправка сообщения"""
        try:
            return await self.application.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview
            )
        except telegram.error.RetryAfter as e:
            logger.warning(f"Rate limit. Retry after {e.retry_after} seconds")
            await asyncio.sleep(e.retry_after)
            return await self._safe_send_message(chat_id, text, reply_markup, parse_mode, disable_web_page_preview)
        except telegram.error.TelegramError as e:
            logger.error(f"Telegram API error: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            return None
    
    async def _safe_edit_message(
        self,
        message: Message,
        text: str,
        reply_markup=None,
        parse_mode=None
    ) -> bool:
        """Безопасное редактирование сообщения"""
        try:
            # Проверяем, отличается ли новый текст от текущего
            if message.text == text and message.reply_markup == reply_markup:
                return True
                
            await message.edit_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=True
            )
            return True
        except telegram.error.BadRequest as e:
            if "message is not modified" in str(e).lower():
                return True
            logger.warning(f"Bad request error: {e}")
            return False
        except telegram.error.RetryAfter as e:
            logger.warning(f"Rate limit. Retry after {e.retry_after} seconds")
            await asyncio.sleep(e.retry_after)
            return await self._safe_edit_message(message, text, reply_markup, parse_mode)
        except telegram.error.TelegramError as e:
            logger.error(f"Telegram API error: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            return False
    

    async def error_handler(self, update: Update, context: CallbackContext) -> None:
        """Обработчик ошибок"""
        logger.error(f"Exception while handling an update: {context.error}", exc_info=True)
        
        try:
            if update.effective_message:
                await self._safe_send_message(
                    chat_id=update.effective_message.chat_id,
                    text="⚠️ Произошла внутренняя ошибка. Попробуйте позже или обратитесь к администратору."
                )
        except Exception as e:
            logger.error(f"Error in error handler: {e}")

    async def initialize(self) -> None:
        """Инициализация приложения бота"""
        #if not self.token or self.token == "8111740535:AAEzEBWQI0rFAdR4gjIGS2SghOOe7oN4L1U":
         #   raise ValueError("Неверный или стандартный токен бота. Пожалуйста, укажите действительный токен.")

        self.application = Application.builder().token(self.token).build()
        
        # Добавляем обработчики
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("menu", self.menu_command))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_message))
        self.application.add_handler(CallbackQueryHandler(self.handle_callback_query))
        self.application.add_error_handler(self.error_handler)

    async def run_async(self) -> None:
        """Асинхронный запуск бота"""
        try:
            await self.initialize()
            logger.info("🚀 Запускаем бота...")
            await self.application.run_polling(drop_pending_updates=True)
        except Exception as e:
            logger.error(f"❌ Критическая ошибка: {e}", exc_info=True)
            raise

    def run(self) -> None:
        """Синхронный запуск бота"""
        try:
            # Проверка токена перед запуском
            #if not self.token or self.token == "8111740535:AAEzEBWQI0rFAdR4gjIGS2SghOOe7oN4L1U":
             #   logger.error("❌ Неверный или стандартный токен бота. Пожалуйста, укажите действительный токен.")
              #  return

            # Применяем nest_asyncio для Jupyter/Colab
            try:
                import nest_asyncio
                nest_asyncio.apply()
                logger.info("✅ nest_asyncio применен")
            except ImportError:
                logger.warning("⚠️ nest_asyncio не найден, может не работать в некоторых средах")

            asyncio.run(self.run_async())
        except KeyboardInterrupt:
            logger.info("🛑 Бот остановлен пользователем")
        except Exception as e:
            logger.error(f"❌ Ошибка при запуске бота: {e}", exc_info=True)

async def main_async():
    """Асинхронная главная функция запуска бота"""
    # Проверяем конфигурацию
    if Config.TOKEN == "8111740535:AAEzEBWQI0rFAdR4gjIGS2SghOOe7oN4L1U":
        logger.error("⚠️ Необходимо указать токен бота в Config.TOKEN")
        return
    
    if not Config.CREDS_FILE:
        logger.error("⚠️ Необходимо указать файл с учетными данными в Config.CREDS_FILE")
        return
    
    if not Config.SHEET_ID:
        logger.error("⚠️ Необходимо указать ID Google Sheets в Config.SHEET_ID")
        return
    
    # Создаем и запускаем бота
    bot = ClinicBot(
        token=Config.TOKEN,
        creds_file=Config.CREDS_FILE,
        sheet_id=Config.SHEET_ID
    )
    
    await bot.run_async()

def main():
    """Главная функция запуска бота"""
    bot = ClinicBot(
        token=Config.TOKEN,
        creds_file=Config.CREDS_FILE,
        sheet_id=Config.SHEET_ID
    )
    bot.run()

import nest_asyncio
nest_asyncio.apply()

if __name__ == "__main__":
    main()
