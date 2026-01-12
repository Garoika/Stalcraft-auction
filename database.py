import sqlite3
import os
import json

class Database:
    def __init__(self, db_path='base.db'):
        self.db_path = db_path
        self.init_db()

    def init_db(self):
        """Инициализация базы данных"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            # Таблица конфигурации
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')

            # Таблица отслеживаемых предметов
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS tracked_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id TEXT,
                    target_price INTEGER DEFAULT 0,
                    target_rarity INTEGER DEFAULT 0
                )
            ''')



            # Таблица истории цен
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id TEXT,
                    time INTEGER,
                    price INTEGER,
                    amount INTEGER,
                    qlt INTEGER DEFAULT 0,
                    UNIQUE(item_id, time, price, amount, qlt)
                )
            ''')

            # Столбец qlt уже добавлен в CREATE TABLE

            # Таблица отслеживаемых строк (для сохранения дубликатов предметов)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS tracked_rows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id TEXT,
                    rarity INTEGER DEFAULT 0
                )
            ''')

            # Индексы для производительности
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_history_item_time ON price_history (item_id, time DESC)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_history_item ON price_history (item_id)')

            conn.commit()

    def get_config(self, key, default=None):
        """Получить значение конфигурации"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT value FROM config WHERE key = ?', (key,))
            result = cursor.fetchone()
            return result[0] if result else default

    def set_config(self, key, value):
        """Установить значение конфигурации"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)
            ''', (key, str(value)))
            conn.commit()



    def add_tracked_item(self, item_id, target_price=0, target_rarity=0):
        """Добавить отслеживаемый предмет"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO tracked_items (item_id, target_price, target_rarity) VALUES (?, ?, ?)
            ''', (item_id, target_price, target_rarity))
            conn.commit()
            return cursor.lastrowid

    def remove_tracked_item(self, row_id):
        """Удалить отслеживаемый предмет"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM tracked_items WHERE id = ?', (row_id,))
            conn.commit()

    def get_tracked_items(self):
        """Получить все отслеживаемые предметы"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id, item_id, target_price, target_rarity FROM tracked_items')
            return cursor.fetchall()

    def update_target_price(self, row_id, price):
        """Обновить целевую цену"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE tracked_items SET target_price = ? WHERE id = ?
            ''', (price, row_id))
            conn.commit()

    def update_target_rarity(self, row_id, rarity):
        """Обновить целевую редкость"""
        print(f"DEBUG: update_target_rarity called for {row_id} with rarity {rarity}")
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE tracked_items SET target_rarity = ? WHERE id = ?
            ''', (rarity, row_id))
            conn.commit()
            print(f"Updated rarity for {row_id} to {rarity}")

    def add_price_history(self, item_id, prices):
        """Добавить записи истории цен"""
        import datetime

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            added_count = 0
            for price_data in prices:
                # Преобразовать время в timestamp если оно строка
                time_val = price_data['time']
                if isinstance(time_val, str):
                    # Предполагаем формат ISO 8601
                    try:
                        dt = datetime.datetime.fromisoformat(time_val.replace('Z', '+00:00'))
                        time_val = int(dt.timestamp())
                    except:
                        # Если не ISO, пробуем как timestamp строку
                        try:
                            time_val = int(float(time_val))
                        except:
                            continue  # Пропускаем некорректные записи

                try:
                    qlt = price_data.get('additional', {}).get('qlt', 0)
                    cursor.execute('''
                        INSERT OR IGNORE INTO price_history (item_id, time, price, amount, qlt)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (item_id, time_val, price_data['price'], price_data['amount'], qlt))

                    if cursor.rowcount > 0:
                        added_count += 1
                except sqlite3.IntegrityError:
                    # Дубликат, пропускаем
                    continue

            # Ограничить до 1000 записей на предмет (самые новые)
            cursor.execute('''
                DELETE FROM price_history WHERE item_id = ? AND id NOT IN (
                    SELECT id FROM price_history WHERE item_id = ? ORDER BY time DESC LIMIT 1000
                )
            ''', (item_id, item_id))

            conn.commit()
            return added_count

    def get_price_history(self, item_id, limit=1000, qlt_filter=None):
        """Получить историю цен для предмета"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            query = '''SELECT time, price, amount, qlt FROM price_history WHERE item_id = ?'''
            params = [item_id]
            if qlt_filter is not None:
                query += ' AND qlt = ?'
                params.append(qlt_filter)
            query += ' ORDER BY time DESC LIMIT ?'
            params.append(limit)
            cursor.execute(query, params)
            return cursor.fetchall()

    def delete_price_history(self, item_id):
        """Удалить всю историю цен для предмета"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM price_history WHERE item_id = ?', (item_id,))
            conn.commit()
            return cursor.rowcount



# Глобальный экземпляр базы данных
db = Database()
