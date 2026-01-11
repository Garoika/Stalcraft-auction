import sys
import os
import json
import requests
import datetime
import time
from PyQt5.QtWidgets import (QApplication, QMainWindow, QVBoxLayout,
                            QWidget, QLabel, QPushButton, QTableWidget,
                            QTableWidgetItem, QLineEdit, QHBoxLayout,
                            QHeaderView, QMessageBox, QDialog,
                            QListWidget, QSpinBox, QTextEdit, QAbstractItemView, QComboBox, QMenu)
from PyQt5.QtCore import Qt, QTimer, QObject, pyqtSignal, QSettings, QThread, QRunnable, QThreadPool, pyqtSlot
from PyQt5.QtGui import QColor

# Импорт базы данных
from database import db

class PageChecker(QRunnable):
    def __init__(self, row, item_id, token, target_price, offset, parent):
        super().__init__()
        self.row = row
        self.item_id = item_id
        self.token = token
        self.target_price = target_price
        self.offset = offset
        self.parent = parent

    @pyqtSlot()
    def run(self):
        try:
            limit = 200
            # Читаем актуальную редкость из таблицы перед каждым запросом
            item = self.parent.table.item(self.row, 0)
            if item is None:
                return
            row_data = item.data(Qt.UserRole)
            rarity = row_data['rarity'] if isinstance(row_data, dict) else 0

            url = f"https://eapi.stalcraft.net/ru/auction/{self.item_id}/lots?sort=buyout_price&order=asc&limit={limit}&offset={self.offset}&additional=true"
            headers = {"Authorization": f"Bearer {self.token}"}
            response = requests.get(url, headers=headers, timeout=15)

            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 5))
                self.parent.error_occurred.emit(f"Лимит запросов. Пауза {retry_after} сек.")
                time.sleep(retry_after)
                # Retry the request
                response = requests.get(url, headers=headers, timeout=15)

            response.raise_for_status()
            data = response.json()

            lots = data.get('lots', [])

            min_price = None
            if lots:
                for index, lot in enumerate(lots):
                    buyout_price = lot.get('buyoutPrice', 0)
                    if buyout_price > 0:
                        # Фильтр по редкости: только лоты выбранной редкости
                        lot_qlt = lot.get('additional', {}).get('qlt', 0)
                        if lot_qlt == rarity:
                            if min_price is None or buyout_price < min_price:
                                min_price = buyout_price

                                    # Check for profitable stack
                            amount = lot.get('amount', 1)
                            if amount > 1:
                                unit_price = buyout_price // amount
                                if self.target_price > 0 and unit_price <= self.target_price:
                                    position = self.offset + index
                                    self.parent.profitable_stack_found.emit(self.item_id, buyout_price, amount, unit_price, position, self.target_price, lot['startTime'], lot['endTime'])

            if min_price is not None:
                self.parent.found_min.emit(self.row, min_price)

            if len(lots) == limit:
                self.parent.next_page.emit(self.row, self.item_id, self.token, self.target_price, self.offset + limit)

        except requests.exceptions.RequestException as e:
            self.parent.error_occurred.emit(f"Ошибка сети для {self.item_id}: {str(e)}")
        except Exception as e:
            self.parent.error_occurred.emit(f"Ошибка для {self.item_id}: {str(e)}")
        finally:
            self.parent.request_finished.emit()


class HistoryLoader(QRunnable):
    def __init__(self, item_id, offset, limit, price_tracker, history_dialog):
        super().__init__()
        self.item_id = item_id
        self.offset = offset
        self.limit = limit
        self.price_tracker = price_tracker
        self.history_dialog = history_dialog

    @pyqtSlot()
    def run(self):
        history = self.price_tracker.fetch_history_page(self.item_id, self.offset, self.limit)
        self.history_dialog.history_loaded.emit(history, self.offset, self.limit)

class HistoryDialog(QDialog):
    history_loaded = pyqtSignal(list, int, int)  # history, offset, limit

    def __init__(self, item_id, name, parent):
        super().__init__(parent)
        self.item_id = item_id
        self.name = name
        self.price_tracker = parent
        self.offset = 0
        self.limit = 200
        self.loading = False
        self.all_history = []
        self.current_filter = 0  # 0 - все, 1-7 - редкости

        self.setWindowTitle(f"История цен: {name}")
        self.resize(800, 600)
        self.setMinimumSize(600, 400)

        layout = QVBoxLayout()

        # Фильтр по редкости
        self.rarity_filter = QComboBox()
        self.rarity_filter.addItems(["Все", "Обычный", "Необычный", "Особый", "Редкий", "Исключительный", "Легендарный"])
        self.rarity_filter.currentIndexChanged.connect(self.on_filter_changed)
        layout.addWidget(self.rarity_filter)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["Время", "Цена", "Количество", "Цена за шт.", "Редкость"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.verticalScrollBar().valueChanged.connect(self.on_scroll)

        layout.addWidget(self.table)

        self.info_label = QLabel("Загрузка...")
        layout.addWidget(self.info_label)

        btn_close = QPushButton("Закрыть")
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close)

        self.setLayout(layout)

        self.history_loaded.connect(self.on_history_loaded)

        # Первоначальная загрузка в фоне
        loader = HistoryLoader(self.item_id, self.offset, self.limit, self.price_tracker, self)
        QThreadPool.globalInstance().start(loader)

    def on_filter_changed(self, index):
        self.current_filter = index
        self.apply_filter()

    def apply_filter(self):
        self.table.setRowCount(0)
        filtered_history = []
        if self.current_filter == 0:  # Все
            filtered_history = self.all_history
        else:
            filter_qlt = self.current_filter - 1  # 0 - обычный, etc.
            filtered_history = [h for h in self.all_history if h.get('additional', {}).get('qlt', 0) == filter_qlt]

        for price_data in filtered_history[-self.limit:]:  # Показать последние limit записей
            row = self.table.rowCount()
            self.table.insertRow(row)
            time_val = price_data['time']
            if isinstance(time_val, str):
                try:
                    dt = datetime.datetime.fromisoformat(time_val.replace('Z', '+00:00'))
                    time_val = dt.timestamp()
                except:
                    time_val = 0
            dt = datetime.datetime.fromtimestamp(time_val)
            time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            self.table.setItem(row, 0, QTableWidgetItem(time_str))
            self.table.setItem(row, 1, QTableWidgetItem(self.price_tracker.format_price(str(price_data['price']))))
            self.table.setItem(row, 2, QTableWidgetItem(str(price_data['amount'])))

            # Цена за шт.
            unit_price = price_data['price'] // price_data['amount'] if price_data['amount'] > 1 else price_data['price']
            self.table.setItem(row, 3, QTableWidgetItem(self.price_tracker.format_price(str(unit_price))))

            # Редкость
            rarity_names = ["Обычный", "Необычный", "Особый", "Редкий", "Исключительный", "Легендарный"]
            qlt = price_data.get('additional', {}).get('qlt', 0)
            rarity_name = rarity_names[qlt] if qlt < len(rarity_names) else f"qlt={qlt}"
            self.table.setItem(row, 4, QTableWidgetItem(rarity_name))

        self.info_label.setText(f"Показаны последние {len(filtered_history[-self.limit:])} записей (фильтр: {self.rarity_filter.currentText()})")

    def load_more_history(self):
        if self.loading:
            return
        self.loading = True
        loader = HistoryLoader(self.item_id, self.offset, self.limit, self.price_tracker, self)
        QThreadPool.globalInstance().start(loader)

    def on_history_loaded(self, history, offset, limit):
        # Сортировка по времени: новые сверху
        history = sorted(history, key=lambda x: x['time'], reverse=True)
        self.all_history.extend(history)
        if offset == 0:
            self.table.setRowCount(0)
        for price_data in history:
            row = self.table.rowCount()
            self.table.insertRow(row)
            time_val = price_data['time']
            if isinstance(time_val, str):
                try:
                    dt = datetime.datetime.fromisoformat(time_val.replace('Z', '+00:00'))
                    time_val = dt.timestamp()
                except:
                    time_val = 0
            dt = datetime.datetime.fromtimestamp(time_val)
            time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            self.table.setItem(row, 0, QTableWidgetItem(time_str))
            self.table.setItem(row, 1, QTableWidgetItem(self.price_tracker.format_price(str(price_data['price']))))
            self.table.setItem(row, 2, QTableWidgetItem(str(price_data['amount'])))

            # Цена за шт.
            unit_price = price_data['price'] // price_data['amount'] if price_data['amount'] > 1 else price_data['price']
            self.table.setItem(row, 3, QTableWidgetItem(self.price_tracker.format_price(str(unit_price))))

            # Редкость
            rarity_names = ["Обычный", "Необычный", "Особый", "Редкий", "Исключительный", "Легендарный"]
            qlt = price_data.get('additional', {}).get('qlt', 0)
            rarity_name = rarity_names[qlt] if qlt < len(rarity_names) else f"qlt={qlt}"
            self.table.setItem(row, 4, QTableWidgetItem(rarity_name))

        self.offset += len(history)
        if len(history) < limit:
            self.info_label.setText(f"Всего записей: {self.table.rowCount()} (конец)")
        else:
            self.info_label.setText(f"Всего записей: {self.table.rowCount()} (прокрутите вниз для загрузки ещё)")
        self.loading = False

        # Если фильтр активен, обновить таблицу
        if self.current_filter != 0:
            self.apply_filter()

    def on_scroll(self, value):
        if not self.loading and value == self.table.verticalScrollBar().maximum():
            self.load_more_history()





class SettingsDialog(QDialog):
    update_db_requested = pyqtSignal()

    def __init__(self, current_interval, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки")
        self.setFixedSize(350, 200)

        layout = QVBoxLayout()

        # --- Interval Section ---
        layout.addWidget(QLabel("Интервал запросов к серверу:"))
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(10, 3600)
        self.interval_spin.setSuffix(" секунд")
        self.interval_spin.setValue(current_interval)
        layout.addWidget(self.interval_spin)

        layout.addSpacing(10)

        # --- Database Update Section ---
        layout.addWidget(QLabel("База данных предметов:"))
        self.update_db_btn = QPushButton("Обновить базу предметов")
        self.update_db_btn.clicked.connect(self.update_db_requested.emit)
        layout.addWidget(self.update_db_btn)

        layout.addStretch()

        # --- Buttons ---
        btn_layout = QHBoxLayout()
        self.save_btn = QPushButton("Сохранить")
        self.save_btn.clicked.connect(self.accept)
        self.cancel_btn = QPushButton("Отмена")
        self.cancel_btn.clicked.connect(self.reject)

        btn_layout.addWidget(self.save_btn)
        btn_layout.addWidget(self.cancel_btn)
        layout.addLayout(btn_layout)

        self.setLayout(layout)

class ItemSearchDialog(QDialog):
    def __init__(self, items_data, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Поиск предмета")
        self.setFixedSize(400, 400)

        self.items_data = items_data
        self.selected_item = None

        layout = QVBoxLayout()

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Введите название предмета...")
        self.search_input.textChanged.connect(self.update_search_results)

        self.results_list = QListWidget()
        self.results_list.itemDoubleClicked.connect(self.select_item)

        self.select_btn = QPushButton("Выбрать")
        self.select_btn.clicked.connect(self.accept_selection)

        layout.addWidget(self.search_input)
        layout.addWidget(self.results_list)
        layout.addWidget(self.select_btn)

        self.setLayout(layout)

        self.all_items = []
        for item in self.items_data:
            try:
                name_ru = item['name']['lines']['ru']
                self.all_items.append((name_ru, item))
            except (KeyError, TypeError):
                continue

    def update_search_results(self, text):
        self.results_list.clear()
        text = text.lower()
        if not text: return

        for name_ru, item in self.all_items:
            if text in name_ru.lower():
                self.results_list.addItem(name_ru)

    def select_item(self, item):
        self.selected_item = None
        for name_ru, item_data in self.all_items:
            if name_ru == item.text():
                self.selected_item = item_data
                break
        self.accept()

    def accept_selection(self):
        current_item = self.results_list.currentItem()
        if current_item:
            self.select_item(current_item)



class PriceTracker(QMainWindow):
    price_checked = pyqtSignal(int, str)
    profitable_stack_found = pyqtSignal(str, int, int, int, int, int, str, str)  # item_id, buyout_price, amount, unit_price, position, target_price, startTime, endTime
    next_page = pyqtSignal(int, str, str, int, int)  # row, item_id, token, target_price, offset
    found_min = pyqtSignal(int, int)  # row, price
    error_occurred = pyqtSignal(str)
    request_finished = pyqtSignal()
    log_message_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()

        if getattr(sys, 'frozen', False):
            self.base_dir = os.path.dirname(sys.executable)
        else:
            self.base_dir = os.path.dirname(os.path.abspath(__file__))

        self.LISTING_FILE = os.path.join(self.base_dir, "listing.json")
        self.LOG_FILE = os.path.join(self.base_dir, "price_tracker.log")

        # Очистка лога при запуске
        with open(self.LOG_FILE, 'w', encoding='utf-8') as f:
            f.write("")

        self.request_interval = 60
        self.running_requests = 0
        self.item_mins = {}
        self.shown_stacks = set()
        self.timer = QTimer()
        self.timer.timeout.connect(self.start_price_check)

        # Связи
        self.price_checked.connect(self.update_item_price)
        self.profitable_stack_found.connect(self.on_profitable_stack)
        self.next_page.connect(self.launch_next_page)
        self.found_min.connect(self.update_min)
        self.error_occurred.connect(self.log_error)
        self.request_finished.connect(self.on_request_finished)
        self.log_message_signal.connect(self.do_log_message)

        self.setWindowTitle("Stalcraft Price Tracker")
        self.setMinimumSize(1000, 700)

        self.init_ui()

        # Миграция данных из файлов в базу данных при первом запуске
        db.migrate_from_files()

        # Первоначальная проверка файлов
        self.ensure_files_exist()
        self.items_data = self.load_item_data()

        self.table.blockSignals(True)
        self.load_settings()
        self.load_tracked_items_from_db()
        self.load_target_prices()
        self.table.blockSignals(False)

        self.log_message("Приложение запущено")
    
    def log_message(self, message):
        self.log_message_signal.emit(message)

    def do_log_message(self, message):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        log_entry = f"[{timestamp}] {message}"

        try:
            with open(self.LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(log_entry + '\n')
        except:
            pass

        self.log_output.append(log_entry)
        self.log_output.verticalScrollBar().setValue(self.log_output.verticalScrollBar().maximum())

    def add_notification(self, message):
        """Добавить уведомление в список"""
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        full_message = f"[{timestamp}] {message}"
        self.notifications_list.insertItem(0, full_message)
        if self.notifications_list.count() > 50:  # Ограничить до 50 уведомлений
            self.notifications_list.takeItem(self.notifications_list.count() - 1)
    
    def format_price(self, price_str):
        try:
            if price_str == "N/A" or not price_str:
                return price_str
                
            clean_str = ''.join(filter(str.isdigit, str(price_str)))
            if not clean_str: return "0 руб."
            price_num = int(clean_str)
            
            formatted = f"{price_num:,}".replace(",", " ")
            return formatted + " руб."
        except (ValueError, TypeError):
            return str(price_str)
    
    def ensure_files_exist(self):
        if not os.path.exists(self.LISTING_FILE) or os.path.getsize(self.LISTING_FILE) < 10:
            self.download_listing_file()

    def merge_uniq_into_listing(self, listing_data):
        """Объединяет данные из uniq.json в listing_data"""
        uniq_file = os.path.join(self.base_dir, "uniq.json")
        if not os.path.exists(uniq_file):
            return listing_data

        try:
            with open(uniq_file, 'r', encoding='utf-8') as f:
                uniq_data = json.load(f)
        except Exception:
            return listing_data

        # Создаем словарь listing по id для быстрого доступа
        listing_dict = {item['id']: item for item in listing_data}

        # Проходим по uniq и обновляем listing
        for uniq_item in uniq_data:
            item_id = uniq_item['itemId']
            if item_id in listing_dict:
                # Обновляем существующий элемент
                existing = listing_dict[item_id]
                # Добавляем новые поля из uniq, кроме id, itemId, name, color
                for key, value in uniq_item.items():
                    if key not in ['id', 'itemId', 'name', 'color']:
                        existing[key] = value
            else:
                # Создаем новый элемент на основе uniq
                new_item = {
                    'id': item_id,
                    'name': {
                        'lines': {
                            'ru': uniq_item['name']
                        }
                    },
                    'color': uniq_item.get('color', 'DEFAULT'),
                    'status': {
                        'state': 'NON_DROP'  # По умолчанию, как в примере
                    }
                }
                # Добавляем остальные поля
                for key, value in uniq_item.items():
                    if key not in ['id', 'itemId', 'name', 'color']:
                        new_item[key] = value
                listing_data.append(new_item)

        return listing_data

    def download_listing_file(self, silent=False):
        url = "https://raw.githubusercontent.com/EXBO-Studio/stalcraft-database/refs/heads/main/ru/listing.json"
        if not silent:
            self.log_message("Синхронизация базы данных предметов (listing.json)...")

        QApplication.setOverrideCursor(Qt.WaitCursor)

        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()

            data = response.json()
            if not isinstance(data, list):
                raise ValueError("Некорректный формат данных")

            # Конвертируем данные: удаляем 'data' и 'icon', добавляем 'id'
            for item in data:
                if 'data' in item:
                    basename = os.path.basename(item['data'])
                    item_id = os.path.splitext(basename)[0]
                    item['id'] = item_id
                    del item['data']
                if 'icon' in item:
                    del item['icon']

            # Объединяем с uniq.json
            data = self.merge_uniq_into_listing(data)

            with open(self.LISTING_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=4)

            self.items_data = data

            if not silent:
                self.log_message("База данных предметов успешно обновлена")
            return True
        except Exception as e:
            error_text = f"Ошибка обновления базы: {str(e)}"
            self.log_message(error_text)
            if not silent:
                QMessageBox.critical(self, "Ошибка", error_text)
            return False
        finally:
            QApplication.restoreOverrideCursor()

    def load_item_data(self):
        try:
            if os.path.exists(self.LISTING_FILE):
                with open(self.LISTING_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        # Проверить, нужно ли объединить с uniq.json
                        uniq_file = os.path.join(self.base_dir, "uniq.json")
                        if os.path.exists(uniq_file):
                            data = self.merge_uniq_into_listing(data)
                            # Сохранить объединенный файл
                            with open(self.LISTING_FILE, 'w', encoding='utf-8') as fw:
                                json.dump(data, fw, ensure_ascii=False, indent=4)
                        return data
            return []
        except Exception as e:
            self.log_message(f"Ошибка чтения listing.json: {str(e)}")
            return []
    
    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout()
        
        # --- Top Panel ---
        top_layout = QHBoxLayout()
        self.settings_btn = QPushButton("⚙")
        self.settings_btn.setFixedWidth(40)
        self.settings_btn.clicked.connect(self.show_settings)
        
        self.token_input = QLineEdit()
        self.token_input.setPlaceholderText("Введите API токен...")
        self.token_input.setEchoMode(QLineEdit.Password)
        self.token_input.textChanged.connect(self.update_token)
        
        top_layout.addWidget(self.settings_btn)
        top_layout.addWidget(QLabel("API Token:"))
        top_layout.addWidget(self.token_input)
        
        # --- Buttons ---
        btn_layout = QHBoxLayout()
        self.btn_add = QPushButton("Добавить предмет")
        self.btn_add.clicked.connect(self.show_item_search)

        self.btn_remove = QPushButton("Удалить предмет")
        self.btn_remove.clicked.connect(self.remove_item)

        self.btn_history = QPushButton("История цен")
        self.btn_history.clicked.connect(self.show_history)

        self.btn_start = QPushButton("Автообновление")
        self.btn_start.clicked.connect(self.toggle_auto_update)

        btn_layout.addWidget(self.btn_add)
        btn_layout.addWidget(self.btn_remove)
        btn_layout.addWidget(self.btn_history)
        btn_layout.addWidget(self.btn_start)

        # --- Middle Area ---
        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Название", "Цена", "Моя цена", "Редкость"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)  # Скрыть нумерацию строк
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked)  # Разрешить редактирование двойным кликом
        self.table.cellChanged.connect(self.on_cell_changed)

        # Уведомления
        self.notifications_list = QListWidget()
        self.notifications_list.setMaximumWidth(300)
        self.notifications_list.setMinimumWidth(200)
        self.notifications_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.notifications_list.customContextMenuRequested.connect(self.show_notification_context_menu)
        self.notifications_list.itemDoubleClicked.connect(self.copy_item_name)

        # Центральный layout для таблицы и уведомлений
        central_layout = QHBoxLayout()
        central_layout.addWidget(self.table, 2)  # stretch 2

        # Правый layout для уведомлений и кнопки
        right_layout = QVBoxLayout()
        right_layout.addWidget(self.notifications_list)
        clear_notifications_btn = QPushButton("Очистить уведомления")
        clear_notifications_btn.clicked.connect(self.clear_notifications)
        right_layout.addWidget(clear_notifications_btn)
        central_layout.addLayout(right_layout, 1)

        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumHeight(150)

        main_layout.addLayout(top_layout)
        main_layout.addLayout(btn_layout)
        main_layout.addWidget(QLabel("Отслеживаемые предметы:"))
        main_layout.addLayout(central_layout)

        main_layout.addWidget(QLabel("Лог:"))
        main_layout.addWidget(self.log_output)
        
        central_widget.setLayout(main_layout)
        
        settings = QSettings("StalcraftTools", "PriceTracker")
        if settings.contains("geometry"):
            self.restoreGeometry(settings.value("geometry"))
    
    def on_cell_changed(self, row, column):
        if column == 2:  # Столбец "Моя цена"
            item = self.table.item(row, column)
            id_item = self.table.item(row, 0)
            if item and id_item:
                row_data = id_item.data(Qt.UserRole)
                if isinstance(row_data, dict):
                    row_id = row_data['id']
                    raw_price = ''.join(filter(str.isdigit, item.text()))

                    if raw_price:
                        self.save_target_price(row_id, raw_price)
                        self.table.blockSignals(True)
                        item.setText(self.format_price(raw_price))
                        self.table.blockSignals(False)
                    else:
                        # Очистить цену
                        self.save_target_price(row_id, 0)
                        self.table.blockSignals(True)
                        item.setText("")
                        self.table.blockSignals(False)
    
    def save_target_price(self, row_id, price):
        try:
            db.update_target_price(row_id, int(price))
        except Exception as e:
            self.log_message(f"Ошибка сохранения целевой цены: {str(e)}")

    def load_tracked_items_from_db(self):
        """Загрузить список отслеживаемых предметов из базы данных"""
        try:
            tracked_items = db.get_tracked_items()
            for id, item_id, _, target_rarity in tracked_items:
                name = self.find_item_name(item_id)
                self.add_item_to_table(item_id, name, existing_id=id, existing_rarity=target_rarity)
        except Exception as e:
            self.log_message(f"Ошибка загрузки списка предметов: {str(e)}")

    def load_target_prices(self):
        try:
            tracked_items = db.get_tracked_items()
            target_data = {id: (target_price, target_rarity) for id, item_id, target_price, target_rarity in tracked_items}

            self.table.blockSignals(True)
            for row in range(self.table.rowCount()):
                id_item = self.table.item(row, 0)
                if id_item:
                    row_data = id_item.data(Qt.UserRole)
                    if isinstance(row_data, dict) and row_data['id'] in target_data:
                        price, rarity = target_data[row_data['id']]
                        if price > 0:
                            self.table.setItem(row, 2, QTableWidgetItem(self.format_price(str(price))))
                            self.table.item(row, 2).setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable)
                        # Обновить combo редкости
                        combo = self.table.cellWidget(row, 3)
                        if combo:
                            combo.blockSignals(True)
                            combo.setCurrentIndex(rarity)
                            combo.blockSignals(False)
                            rarity_colors = ["white", "green", "blue", "purple", "red", "gold"]
                            combo.setStyleSheet(f"QComboBox {{ background-color: {rarity_colors[rarity]}; }}")
            self.table.blockSignals(False)
        except Exception as e:
            self.log_message(f"Ошибка загрузки целевых цен: {str(e)}")
    


    def update_item_price(self, row, price):
        try:
            formatted_price = self.format_price(price)
            self.table.blockSignals(True)
            price_item = self.table.item(row, 1)
            if price_item: price_item.setText(formatted_price)
            self.table.blockSignals(False)

            id_item = self.table.item(row, 0)
            name_item = self.table.item(row, 0)
            name_text = name_item.text() if name_item else ""
            target_item = self.table.item(row, 2)  # "Моя цена"

            if id_item and name_text and target_item and target_item.text():
                try:
                    current_price = int(''.join(filter(str.isdigit, price)))
                    target_price = int(''.join(filter(str.isdigit, target_item.text())))

                    if current_price > 0 and current_price <= target_price:
                        message = f"🚀 ВЫГОДНО: {name_text} за {formatted_price}"
                        self.log_message(message)
                        notification_message = f"{name_text}\n{formatted_price}"
                        self.add_notification(notification_message)
                        for col in range(self.table.columnCount()):
                            cell = self.table.item(row, col)
                            if cell:  # Проверяем, что ячейка существует
                                cell.setBackground(QColor(255, 255, 0))

                        QApplication.beep()
                        row_data = id_item.data(Qt.UserRole)
                        QTimer.singleShot(30000, lambda: self.reset_row_color(row_data))
                    else:
                        self.reset_row_color(id_item.data(Qt.UserRole))
                except ValueError: pass
        except Exception as e:
            self.log_message(f"Ошибка при обновлении цены: {str(e)}")

    def on_profitable_stack(self, item_id, buyout_price, amount, unit_price, position, target_price, startTime, endTime):
        token = f"{item_id}_{buyout_price}_{amount}_{startTime}"
        if token not in self.shown_stacks:
            self.shown_stacks.add(token)
            profit = (amount * target_price) - buyout_price
            name = self.find_item_name(item_id)
            page = position // 50 + 1
            formatted_total = self.format_price(str(buyout_price))
            formatted_unit = self.format_price(str(unit_price))
            message = f"💰 ВЫГОДНЫЙ СТАК: {name} - {amount} шт. за {formatted_total} ({formatted_unit} за шт.) - Прибыль: {profit}"
            notification_message = f"{name} (x{amount})\nЦена за стак: {buyout_price}\nЦена за шт.: {unit_price}\nСтраница {page}"
            self.add_notification(notification_message)
            QApplication.beep()

    def launch_next_page(self, row, item_id, token, target_price, offset):
        runnable = PageChecker(row, item_id, token, target_price, offset, self)
        self.running_requests += 1
        QThreadPool.globalInstance().start(runnable)

    def update_min(self, row, price):
        if row not in self.item_mins or price < self.item_mins[row]:
            self.item_mins[row] = price
    
    def reset_row_color(self, row_data):
        """Сбросить цвет строки обратно к белому"""
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.data(Qt.UserRole) is row_data:
                for col in range(self.table.columnCount()):
                    cell = self.table.item(row, col)
                    if cell:
                        cell.setBackground(QColor(Qt.white))
                break

    def on_rarity_changed_by_id(self, row, combo):
        item = self.table.item(row, 0)
        if item:
            row_data = item.data(Qt.UserRole)
            if isinstance(row_data, dict):
                row_id = row_data['id']
                item_id = row_data['item_id']
                rarity = combo.currentIndex()
                rarity_names = ["Обычный", "Необычный", "Особый", "Редкий", "Исключительный", "Легендарный"]
                item_name = self.find_item_name(item_id)
                self.log_message(f"Редкость для {item_name} изменена на {rarity_names[rarity]}")
                # Обновить цвет
                rarity_colors = ["white", "green", "blue", "purple", "red", "gold"]
                combo.setStyleSheet(f"QComboBox {{ background-color: {rarity_colors[rarity]}; }}")
                db.update_target_rarity(row_id, rarity)
                # Обновить UserRole
                row_data['rarity'] = rarity
                item.setData(Qt.UserRole, row_data)

    def update_token(self):
        token = self.token_input.text().strip()

    def load_settings(self):
        try:
            self.request_interval = int(db.get_config('interval', '60'))
            token = db.get_config('token', '')
            if token:
                self.token_input.setText(token)
                self.update_token()
        except: pass

    def save_settings(self):
        try:
            db.set_config('interval', str(self.request_interval))
            db.set_config('token', self.token_input.text().strip())
        except: pass

    def show_settings(self):
        dialog = SettingsDialog(self.request_interval)
        dialog.update_db_requested.connect(lambda: self.handle_manual_update(dialog))

        if dialog.exec_() == QDialog.Accepted:
            self.request_interval = dialog.interval_spin.value()
            self.save_settings()
            if self.timer.isActive(): self.timer.start(self.request_interval * 1000)
            self.log_message(f"Интервал изменен: {self.request_interval} сек")

    def handle_manual_update(self, dialog):
        success = self.download_listing_file(silent=False)
        if success:
            QMessageBox.information(dialog, "Успех", "База данных предметов успешно обновлена!")

    def show_item_search(self):
        if not self.items_data:
            self.items_data = self.load_item_data()
            if not self.items_data:
                QMessageBox.warning(self, "Ошибка", "База данных предметов пуста. Обновите её в настройках.")
                return

        dialog = ItemSearchDialog(self.items_data, self)
        if dialog.exec_() == QDialog.Accepted and dialog.selected_item:
            item_data = dialog.selected_item
            item_id = item_data['id']
            self.add_item_to_table(item_id, item_data['name']['lines']['ru'])
    




    def add_item_to_table(self, item_id, name, existing_id=None, existing_rarity=0):
        # Always add to database
        if existing_id is None:
            row_id = db.add_tracked_item(item_id)
        else:
            row_id = existing_id

        self.table.blockSignals(True)
        row = self.table.rowCount()
        self.table.insertRow(row)

        self.table.setItem(row, 0, QTableWidgetItem(name))
        self.table.item(row, 0).setData(Qt.UserRole, {'id': row_id, 'item_id': item_id, 'rarity': existing_rarity})
        self.table.item(row, 0).setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)

        self.table.setItem(row, 1, QTableWidgetItem("---"))
        self.table.item(row, 1).setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)

        self.table.setItem(row, 2, QTableWidgetItem(""))
        self.table.item(row, 2).setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable)

        # Столбец 3 - Редкость
        combo = QComboBox()
        combo.addItems(["Обычный", "Необычный", "Особый", "Редкий", "Исключительный", "Легендарный"])
        combo.setCurrentIndex(existing_rarity)  # Use existing_rarity
        combo.setEnabled(True)
        combo.setFocusPolicy(Qt.StrongFocus)
        combo.setStyleSheet("QComboBox { background-color: white; }")
        combo.currentIndexChanged.connect(lambda index, row=row, combo=combo: self.on_rarity_changed_by_id(row, combo))
        self.table.setCellWidget(row, 3, combo)

        self.table.blockSignals(False)
    
    def remove_item(self):
        selected = self.table.currentRow()
        if selected == -1: return
        row_data = self.table.item(selected, 0).data(Qt.UserRole)
        if isinstance(row_data, dict):
            row_id = row_data['id']
            item_id = row_data['item_id']
            self.table.removeRow(selected)
            db.remove_tracked_item(row_id)
            self.log_message(f"Удалён предмет {item_id}")


    
    def find_item_name(self, item_id):
        for item in self.items_data:
            try:
                current_id = item.get('id', '')
                if current_id == item_id: return item['name']['lines']['ru']
            except: continue
        return item_id
    

    
    def start_price_check(self):
        if not self.token_input.text().strip() or self.table.rowCount() == 0: return

        token = self.token_input.text().strip()
        thread_pool = QThreadPool.globalInstance()
        self.running_requests = 0
        self.item_mins = {}

        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item:
                row_data = item.data(Qt.UserRole)
                if isinstance(row_data, dict):
                    item_id = row_data['item_id']
                    target_item = self.table.item(r, 2)
                    target_price = 0
                    if target_item and target_item.text():
                        target_price = int(''.join(filter(str.isdigit, target_item.text())))
                    self.running_requests += 1
                    runnable = PageChecker(r, item_id, token, target_price, 0, self)
                    thread_pool.start(runnable)

        # Очистить показанные стаки только если нет активных запросов
        if self.running_requests == 0:
            self.shown_stacks.clear()

    def on_request_finished(self):
        self.running_requests -= 1
        if self.running_requests == 0:
            self.on_check_complete()

    def on_check_complete(self):
        self.log_message("Проверка цен завершена")
        for row, price in self.item_mins.items():
            self.price_checked.emit(row, str(price))
        self.item_mins = {}



    def show_history(self):
        try:
            selected = self.table.currentRow()
            if selected == -1:
                QMessageBox.warning(self, "Ошибка", "Выберите предмет в таблице!")
                return

            row_data = self.table.item(selected, 0).data(Qt.UserRole)
            if isinstance(row_data, dict):
                item_id = row_data['item_id']
                name = self.table.item(selected, 0).text()

                dialog = HistoryDialog(item_id, name, self)
                dialog.exec_()
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось открыть историю: {str(e)}")
            self.log_message(f"Ошибка при открытии истории: {str(e)}")

    def fetch_history_page(self, item_id, offset=0, limit=200):
        """Загрузить страницу истории цен для предмета"""
        try:
            token = self.token_input.text().strip()
            if not token:
                return []

            url = f"https://eapi.stalcraft.net/ru/auction/{item_id}/history"
            headers = {"Authorization": f"Bearer {token}"}
            params = {"limit": limit, "offset": offset, "additional": "true"}

            response = requests.get(url, headers=headers, params=params, timeout=15)

            if response.status_code == 200:
                return response.json().get('prices', [])
            else:
                self.log_message(f"Ошибка загрузки истории для {item_id}: {response.status_code}")
                return []
        except Exception as e:
            self.log_message(f"Ошибка при загрузке истории {item_id}: {str(e)}")
            return []



    def log_error(self, error_msg):
        self.log_message(f"ОШИБКА: {error_msg}")

    def clear_notifications(self):
        self.log_message("Уведомления очищены")
        self.notifications_list.clear()
        self.shown_stacks.clear()

    def copy_item_name(self, item):
        text = item.text()
        # Убрать timestamp: text после '] '
        if '] ' in text:
            message = text.split('] ', 1)[1]
        else:
            message = text
        # Извлечь название из первой строки
        name = message.split('\n')[0]
        # Убрать (x{amount}) если есть
        if ' (' in name and name.endswith(')'):
            name = name.split(' (')[0]
        QApplication.clipboard().setText(name)
        self.log_message(f"Название '{name}' скопировано в буфер обмена")

    def show_notification_context_menu(self, position):
        menu = QMenu()
        buy_action = menu.addAction("✅ Купил")
        buy_action.triggered.connect(lambda: self.mark_notification_bought(self.notifications_list.currentRow()))
        menu.exec_(self.notifications_list.mapToGlobal(position))

    def mark_notification_bought(self, row):
        if row >= 0:
            self.notifications_list.takeItem(row)
    
    def toggle_auto_update(self):
        if self.timer.isActive():
            self.timer.stop()
            self.btn_start.setText("Автообновление")
            self.log_message("Автообновление остановлено")
        else:
            if not self.token_input.text().strip():
                QMessageBox.warning(self, "Ошибка", "Введите токен!")
                return
            self.timer.start(self.request_interval * 1000)
            self.btn_start.setText("Остановить")
            self.log_message(f"Цикл запущен ({self.request_interval}с) - проверка цен и поиск выгодных стаков")
            self.start_price_check()

    def closeEvent(self, event):
        self.save_settings()
        settings = QSettings("StalcraftTools", "PriceTracker")
        settings.setValue("geometry", self.saveGeometry())
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = PriceTracker()
    window.show()
    sys.exit(app.exec_())
