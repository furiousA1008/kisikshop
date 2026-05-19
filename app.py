import os
import hashlib
import secrets
import logging
import json
import random
import string
import time
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, request, jsonify, render_template, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, text
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import requests
from io import BytesIO
from PIL import Image
import threading

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ============ КОНФІГУРАЦІЯ БАЗИ ДАНИХ ============
# Підтримка PostgreSQL (Render) та SQLite (локально / з диском)
DATA_DIR = '/data' if os.path.exists('/data') else os.path.dirname(os.path.abspath(__file__))
os.makedirs(DATA_DIR, exist_ok=True)

DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL and DATABASE_URL.startswith('postgres'):
    # Використовуємо PostgreSQL на Render
    app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
    logger.info(f"Using PostgreSQL database")
else:
    # Використовуємо SQLite (локально або на Render з диском)
    db_path = os.path.join(DATA_DIR, 'shop.db')
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
    logger.info(f"Using SQLite database at: {db_path}")

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = secrets.token_hex(32)
app.config['PERMANENT_SESSION_LIFETIME'] = 86400
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024

db = SQLAlchemy(app)

# ============ TELEGRAM КОНФІГ ============
TELEGRAM_SUPPORT_CHAT_ID = "807319863"
TELEGRAM_BOT_LINK = "https://t.me/kisik_support_bot"

# ============ CLOUDFLARE R2 КОНФІГУРАЦІЯ ============
R2_ACCOUNT_ID = "71580147ba20088caf6fa682a7e8edf2"
R2_PUBLIC_ACCOUNT_ID = "1fdfbb000ddb47c083bd7d336943d068"
R2_ACCESS_KEY_ID = "cd6e48c0c7871bbea9f62a8337a316c1"
R2_SECRET_ACCESS_KEY = "e93054ed70b31327ef3f9faa7a8b704ec8f121eee5122f80ff23b1c1f0dabd80"
R2_BUCKET_NAME = "kisikmarket"
R2_PUBLIC_URL_BASE = f"https://pub-{R2_PUBLIC_ACCOUNT_ID}.r2.dev"

s3_client = boto3.client(
    's3',
    endpoint_url=f'https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=Config(signature_version='s3v4', region_name='auto'),
    verify=True
)

# ============ TELEGRAM БОТ ============
BOT_TOKEN = "8599704700:AAGQuJTOTB-36FGtk66PAVce0x-CSy9PE0c"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

ADMIN_CHAT_IDS = [
    807319863,
    1092098458,
]

def send_telegram_message(text, chat_id):
    """Відправляє текстове повідомлення в Telegram"""
    try:
        data = {
            "chat_id": chat_id,
            "text": text[:4096],
            "parse_mode": "HTML"
        }
        response = requests.post(f"{TELEGRAM_API}/sendMessage", json=data, timeout=10)
        if response.status_code == 200:
            logger.info(f"Message sent to {chat_id}")
            return True
        else:
            logger.error(f"Telegram error: {response.text}")
            return False
    except Exception as e:
        logger.error(f"Telegram send error to {chat_id}: {e}")
        return False

def send_order_notification(order_id, items_data):
    """Відправляє текстове сповіщення про замовлення"""
    with app.app_context():
        try:
            order = db.session.get(Order, order_id)
            if not order:
                logger.error(f"Order {order_id} not found")
                return

            base_url = "https://kisikshop.onrender.com/"

            payment_text = {
                'full': '💳 Повна оплата',
                'partial': '💸 Часткова оплата (30%)',
                'post': '📦 Оплата на пошті'
            }.get(order.payment_method, '💳 Повна оплата')

            message = f"""🛍️ <b>НОВЕ ЗАМОВЛЕННЯ!</b>

<b>📦 Номер:</b> {order.order_number}
<b>👤 Клієнт:</b> {order.user_name}
<b>📞 Телефон:</b> <code>{order.user_phone}</code>
{f'📧 Email: {order.user_email}' if order.user_email else ''}
<b>📍 Місто:</b> {order.city}
🏠 <b>Адреса:</b> {order.warehouse_address if order.warehouse_address else 'Не вказано'}
<b>💰 Сума:</b> {order.total_price} грн
<b>💳 Оплата:</b> {payment_text}

<b>📋 Товари:</b>
"""
            for idx, item in enumerate(items_data, 1):
                color_text = f" ({item.get('color', 'стандарт')})" if item.get('color') else ''
                product_link = f"{base_url}/product/{item['id']}"
                message += f"\n{idx}. <b><a href='{product_link}'>{item['name']}</a></b>{color_text}\n   📏 Розмір: {item['size']} | 🔢 Кількість: {item['quantity']} | 💰 {item['total']} грн"

            for chat_id in ADMIN_CHAT_IDS:
                send_telegram_message(message, chat_id)

        except Exception as e:
            logger.error(f"Error sending order notification: {e}")

# ============ ФУНКЦІЇ R2 ============
def optimize_image(image_data, max_size=(1200, 1200), quality=85):
    try:
        img = Image.open(BytesIO(image_data))
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        img.thumbnail(max_size, Image.Resampling.LANCZOS)
        buffer = BytesIO()
        img.save(buffer, format='JPEG', quality=quality, optimize=True)
        buffer.seek(0)
        return buffer.getvalue()
    except Exception as e:
        logger.error(f"Image optimization error: {e}")
        return image_data

def upload_photo_to_r2(file_data, filename):
    try:
        if not file_data:
            raise Exception("No file data provided")

        optimized_data = optimize_image(file_data)

        if len(optimized_data) > 10 * 1024 * 1024:
            raise Exception("File too large. Max 10MB")

        timestamp = int(time.time() * 1000)
        ext = 'jpg'
        unique_filename = f"products/{timestamp}_{secrets.token_hex(8)}.{ext}"

        s3_client.put_object(
            Bucket=R2_BUCKET_NAME,
            Key=unique_filename,
            Body=optimized_data,
            ContentType='image/jpeg',
            CacheControl='public, max-age=31536000'
        )
        photo_url = f"{R2_PUBLIC_URL_BASE}/{unique_filename}"
        logger.info(f"Photo uploaded to R2: {unique_filename}")
        return photo_url
    except Exception as e:
        logger.error(f"Upload error: {e}")
        raise

def upload_size_chart_to_r2(file_data, filename):
    try:
        if not file_data:
            raise Exception("No file data provided")

        optimized_data = optimize_image(file_data)

        if len(optimized_data) > 10 * 1024 * 1024:
            raise Exception("File too large. Max 10MB")

        timestamp = int(time.time() * 1000)
        ext = 'jpg'
        unique_filename = f"size_charts/{timestamp}_{secrets.token_hex(8)}.{ext}"
        s3_client.put_object(
            Bucket=R2_BUCKET_NAME,
            Key=unique_filename,
            Body=optimized_data,
            ContentType='image/jpeg',
            CacheControl='public, max-age=31536000'
        )
        photo_url = f"{R2_PUBLIC_URL_BASE}/{unique_filename}"
        logger.info(f"Size chart uploaded to R2: {unique_filename}")
        return photo_url
    except Exception as e:
        logger.error(f"Upload error: {e}")
        raise

def delete_photo_from_r2(photo_url):
    try:
        if not photo_url:
            return False
        key = None
        if R2_PUBLIC_URL_BASE in photo_url:
            key = photo_url.replace(f"{R2_PUBLIC_URL_BASE}/", "")
        elif 'r2.dev' in photo_url:
            parts = photo_url.split('/')
            if len(parts) >= 4:
                key = '/'.join(parts[3:])
        if key:
            key = key.lstrip('/')
            s3_client.delete_object(Bucket=R2_BUCKET_NAME, Key=key)
            logger.info(f"Photo deleted from R2: {key}")
            return True
        return False
    except Exception as e:
        logger.error(f"Delete error: {e}")
        return False

# ============ МОДЕЛІ ============
class Category(db.Model):
    __tablename__ = 'categories'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    icon = db.Column(db.String(10), default='🏷️')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Color(db.Model):
    __tablename__ = 'colors'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    code = db.Column(db.String(20), default='#000000')
    icon = db.Column(db.String(10), default='🎨')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    phone = db.Column(db.String(20), nullable=True)
    email = db.Column(db.String(100), nullable=True)

class Product(db.Model):
    __tablename__ = 'products'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(50), nullable=False)
    price = db.Column(db.Float, nullable=False)
    sizes = db.Column(db.String(100), nullable=False)
    colors = db.Column(db.String(200), default='')
    description = db.Column(db.Text, default='')
    brand = db.Column(db.String(100), default='KISIK')
    material = db.Column(db.String(200), default='')
    country = db.Column(db.String(100), default='Україна')
    photo_urls = db.Column(db.Text)
    size_chart_url = db.Column(db.String(500), nullable=True)
    rating_sum = db.Column(db.Integer, default=0)
    rating_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def rating_avg(self):
        if self.rating_count == 0:
            return 0
        return round(self.rating_sum / self.rating_count, 1)

class Review(db.Model):
    __tablename__ = 'reviews'
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    user_name = db.Column(db.String(100), nullable=False)
    rating = db.Column(db.Integer, nullable=False)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Favorite(db.Model):
    __tablename__ = 'favorites'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('user_id', 'product_id', name='unique_favorite'),)

class Order(db.Model):
    __tablename__ = 'orders'
    id = db.Column(db.Integer, primary_key=True)
    order_number = db.Column(db.String(20), unique=True, nullable=False)
    user_id = db.Column(db.Integer, nullable=True)
    guest_session = db.Column(db.String(100), nullable=True)
    user_name = db.Column(db.String(100), nullable=False)
    user_phone = db.Column(db.String(20), nullable=False)
    user_email = db.Column(db.String(100), nullable=True)
    city = db.Column(db.String(100), nullable=False)
    warehouse_address = db.Column(db.String(500), nullable=True)
    delivery_method = db.Column(db.String(50), default='nova_poshta')
    payment_method = db.Column(db.String(50), default='full')
    selected_color = db.Column(db.String(50), nullable=True)
    total_price = db.Column(db.Float, nullable=False)
    items = db.Column(db.Text)
    status = db.Column(db.String(20), default='new')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

class CartItem(db.Model):
    __tablename__ = 'cart_items'
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(100), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    size = db.Column(db.String(10), nullable=False)
    color = db.Column(db.String(50), nullable=True)
    quantity = db.Column(db.Integer, default=1)

# ============ ДОПОМІЖНІ ФУНКЦІЇ ============
def generate_order_number():
    while True:
        num = ''.join(random.choices(string.digits, k=10))
        if not Order.query.filter_by(order_number=num).first():
            return num

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def check_password(password, hashed):
    return hash_password(password) == hashed

def get_cart_session_id():
    if 'cart_session_id' not in session:
        session['cart_session_id'] = secrets.token_hex(16)
    return session['cart_session_id']

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Unauthorized'}), 401
        user = User.query.get(session['user_id'])
        if not user or not user.is_admin:
            return jsonify({'error': 'Forbidden'}), 403
        return f(*args, **kwargs)
    return decorated

def is_admin():
    if 'user_id' in session:
        user = User.query.get(session['user_id'])
        return user and user.is_admin
    return False

def migrate_database():
    """Міграція бази даних - додає нові колонки якщо їх немає"""
    try:
        # Users table
        result = db.session.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")).fetchone()
        if result:
            columns = db.session.execute(text("PRAGMA table_info(users)")).fetchall()
            column_names = [col[1] for col in columns]
            if 'phone' not in column_names:
                db.session.execute(text("ALTER TABLE users ADD COLUMN phone VARCHAR(20)"))
            if 'email' not in column_names:
                db.session.execute(text("ALTER TABLE users ADD COLUMN email VARCHAR(100)"))
            db.session.commit()

        # Products table
        result = db.session.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='products'")).fetchone()
        if result:
            columns = db.session.execute(text("PRAGMA table_info(products)")).fetchall()
            column_names = [col[1] for col in columns]
            if 'description' not in column_names:
                db.session.execute(text("ALTER TABLE products ADD COLUMN description TEXT"))
            if 'brand' not in column_names:
                db.session.execute(text("ALTER TABLE products ADD COLUMN brand VARCHAR(100) DEFAULT 'KISIK'"))
            if 'material' not in column_names:
                db.session.execute(text("ALTER TABLE products ADD COLUMN material VARCHAR(200)"))
            if 'country' not in column_names:
                db.session.execute(text("ALTER TABLE products ADD COLUMN country VARCHAR(100) DEFAULT 'Україна'"))
            if 'colors' not in column_names:
                db.session.execute(text("ALTER TABLE products ADD COLUMN colors VARCHAR(200) DEFAULT ''"))
            if 'size_chart_url' not in column_names:
                db.session.execute(text("ALTER TABLE products ADD COLUMN size_chart_url VARCHAR(500)"))
            if 'rating_sum' not in column_names:
                db.session.execute(text("ALTER TABLE products ADD COLUMN rating_sum INTEGER DEFAULT 0"))
            if 'rating_count' not in column_names:
                db.session.execute(text("ALTER TABLE products ADD COLUMN rating_count INTEGER DEFAULT 0"))
            db.session.commit()

        # Orders table
        result = db.session.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='orders'")).fetchone()
        if result:
            columns = db.session.execute(text("PRAGMA table_info(orders)")).fetchall()
            column_names = [col[1] for col in columns]
            if 'guest_session' not in column_names:
                db.session.execute(text("ALTER TABLE orders ADD COLUMN guest_session VARCHAR(100)"))
            if 'user_email' not in column_names:
                db.session.execute(text("ALTER TABLE orders ADD COLUMN user_email VARCHAR(100)"))
            if 'delivery_method' not in column_names:
                db.session.execute(text("ALTER TABLE orders ADD COLUMN delivery_method VARCHAR(50) DEFAULT 'nova_poshta'"))
            if 'warehouse_address' not in column_names:
                db.session.execute(text("ALTER TABLE orders ADD COLUMN warehouse_address VARCHAR(500)"))
            if 'payment_method' not in column_names:
                db.session.execute(text("ALTER TABLE orders ADD COLUMN payment_method VARCHAR(50) DEFAULT 'full'"))
            if 'selected_color' not in column_names:
                db.session.execute(text("ALTER TABLE orders ADD COLUMN selected_color VARCHAR(50)"))
            db.session.commit()

        # Cart items
        result = db.session.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='cart_items'")).fetchone()
        if result:
            columns = db.session.execute(text("PRAGMA table_info(cart_items)")).fetchall()
            column_names = [col[1] for col in columns]
            if 'color' not in column_names:
                db.session.execute(text("ALTER TABLE cart_items ADD COLUMN color VARCHAR(50)"))
            db.session.commit()

        # Create reviews table if not exists
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                user_id INTEGER,
                user_name VARCHAR(100) NOT NULL,
                rating INTEGER NOT NULL,
                comment TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (product_id) REFERENCES products(id),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """))

        # Create favorites table if not exists
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (product_id) REFERENCES products(id),
                UNIQUE(user_id, product_id)
            )
        """))

        db.session.commit()
        logger.info("Database migration completed")
    except Exception as e:
        logger.error(f"Migration error: {e}")

# ============ API МАРШРУТИ ============

# Категорії
@app.route('/api/categories', methods=['GET'])
def get_categories():
    categories = Category.query.order_by(Category.name).all()
    return jsonify([{'id': c.id, 'name': c.name, 'icon': c.icon} for c in categories])

@app.route('/api/categories', methods=['POST'])
@admin_required
def create_category():
    data = request.json
    name = data.get('name', '').strip()
    icon = data.get('icon', '🏷️')
    if not name:
        return jsonify({'error': 'Назва категорії обов\'язкова'}), 400
    if Category.query.filter_by(name=name).first():
        return jsonify({'error': 'Категорія вже існує'}), 400
    category = Category(name=name, icon=icon)
    db.session.add(category)
    db.session.commit()
    return jsonify({'id': category.id})

@app.route('/api/categories/<int:category_id>', methods=['DELETE'])
@admin_required
def delete_category(category_id):
    category = Category.query.get(category_id)
    if not category:
        return jsonify({'error': 'Категорію не знайдено'}), 404
    db.session.delete(category)
    db.session.commit()
    return jsonify({'status': 'ok'})

# Кольори
@app.route('/api/colors', methods=['GET'])
def get_colors():
    colors = Color.query.order_by(Color.name).all()
    return jsonify([{'id': c.id, 'name': c.name, 'code': c.code, 'icon': c.icon} for c in colors])

@app.route('/api/colors', methods=['POST'])
@admin_required
def create_color():
    data = request.json
    name = data.get('name', '').strip()
    code = data.get('code', '#000000')
    icon = data.get('icon', '🎨')
    if not name:
        return jsonify({'error': 'Назва кольору обов\'язкова'}), 400
    if Color.query.filter_by(name=name).first():
        return jsonify({'error': 'Колір вже існує'}), 400
    color = Color(name=name, code=code, icon=icon)
    db.session.add(color)
    db.session.commit()
    return jsonify({'id': color.id})

@app.route('/api/colors/<int:color_id>', methods=['DELETE'])
@admin_required
def delete_color(color_id):
    color = Color.query.get(color_id)
    if not color:
        return jsonify({'error': 'Колір не знайдено'}), 404
    db.session.delete(color)
    db.session.commit()
    return jsonify({'status': 'ok'})

# Авторизація
@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    if not username or not password:
        return jsonify({'error': 'Заповніть всі поля'}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({'error': 'Користувач вже існує'}), 400
    user = User(username=username, password=hash_password(password))
    db.session.add(user)
    db.session.commit()
    session['user_id'] = user.id
    return jsonify({'status': 'ok', 'user': {'id': user.id, 'username': user.username, 'is_admin': user.is_admin}})

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    user = User.query.filter_by(username=username).first()
    if not user or not check_password(password, user.password):
        return jsonify({'error': 'Невірний логін або пароль'}), 401
    session['user_id'] = user.id
    return jsonify({'status': 'ok', 'user': {'id': user.id, 'username': user.username, 'is_admin': user.is_admin}})

@app.route('/api/logout', methods=['POST'])
def logout():
    session.pop('user_id', None)
    return jsonify({'status': 'ok'})

@app.route('/api/me', methods=['GET'])
def get_me():
    if 'user_id' not in session:
        return jsonify({'user': None})
    user = User.query.get(session['user_id'])
    if not user:
        return jsonify({'user': None})
    return jsonify({'user': {'id': user.id, 'username': user.username, 'is_admin': user.is_admin}})

# Відгуки
@app.route('/api/products/<int:product_id>/reviews', methods=['GET'])
def get_product_reviews(product_id):
    reviews = Review.query.filter_by(product_id=product_id).order_by(Review.created_at.desc()).all()
    return jsonify([{
        'id': r.id, 'user_name': r.user_name, 'rating': r.rating,
        'comment': r.comment, 'created_at': r.created_at.isoformat()
    } for r in reviews])

@app.route('/api/products/<int:product_id>/reviews', methods=['POST'])
@login_required
def add_review(product_id):
    product = Product.query.get(product_id)
    if not product:
        return jsonify({'error': 'Товар не знайдено'}), 404

    data = request.json
    rating = data.get('rating')
    comment = data.get('comment', '')

    if not rating or rating < 1 or rating > 5:
        return jsonify({'error': 'Рейтинг має бути від 1 до 5'}), 400

    user = User.query.get(session['user_id'])

    review = Review(
        product_id=product_id, user_id=user.id,
        user_name=user.username, rating=rating, comment=comment
    )

    product.rating_sum += rating
    product.rating_count += 1

    db.session.add(review)
    db.session.commit()

    return jsonify({'status': 'ok', 'rating_avg': product.rating_avg, 'rating_count': product.rating_count})

# Вподобання
@app.route('/api/favorites', methods=['GET'])
@login_required
def get_favorites():
    favorites = Favorite.query.filter_by(user_id=session['user_id']).all()
    return jsonify([f.product_id for f in favorites])

@app.route('/api/favorites/<int:product_id>', methods=['POST'])
@login_required
def add_favorite(product_id):
    product = Product.query.get(product_id)
    if not product:
        return jsonify({'error': 'Товар не знайдено'}), 404

    existing = Favorite.query.filter_by(user_id=session['user_id'], product_id=product_id).first()
    if existing:
        return jsonify({'status': 'ok', 'favorited': True})

    favorite = Favorite(user_id=session['user_id'], product_id=product_id)
    db.session.add(favorite)
    db.session.commit()
    return jsonify({'status': 'ok', 'favorited': True})

@app.route('/api/favorites/<int:product_id>', methods=['DELETE'])
@login_required
def remove_favorite(product_id):
    favorite = Favorite.query.filter_by(user_id=session['user_id'], product_id=product_id).first()
    if favorite:
        db.session.delete(favorite)
        db.session.commit()
    return jsonify({'status': 'ok', 'favorited': False})

# Товари
@app.route('/api/products', methods=['GET'])
def get_products():
    category = request.args.get('category')
    query = Product.query
    if category and category != 'all':
        query = query.filter_by(category=category)
    products = query.order_by(Product.created_at.desc()).all()
    return jsonify([{
        'id': p.id, 'name': p.name, 'category': p.category, 'price': p.price,
        'sizes': p.sizes.split(','), 'colors': p.colors.split(',') if p.colors else [],
        'description': p.description, 'brand': p.brand, 'material': p.material,
        'country': p.country, 'photos': p.photo_urls.split(',') if p.photo_urls else [],
        'size_chart_url': p.size_chart_url, 'rating_avg': p.rating_avg,
        'rating_count': p.rating_count, 'created_at': p.created_at.isoformat()
    } for p in products])

@app.route('/api/products/<int:product_id>', methods=['GET'])
def get_product(product_id):
    product = db.session.get(Product, product_id)
    if not product:
        return jsonify({'error': 'Товар не знайдено'}), 404
    return jsonify({
        'id': product.id, 'name': product.name, 'category': product.category,
        'price': product.price, 'sizes': product.sizes.split(','),
        'colors': product.colors.split(',') if product.colors else [],
        'description': product.description, 'brand': product.brand,
        'material': product.material, 'country': product.country,
        'photos': product.photo_urls.split(',') if product.photo_urls else [],
        'size_chart_url': product.size_chart_url, 'rating_avg': product.rating_avg,
        'rating_count': product.rating_count, 'created_at': product.created_at.isoformat()
    })

@app.route('/api/search', methods=['GET'])
def search_products():
    query = request.args.get('q', '').strip()
    if not query or len(query) < 2:
        return jsonify([])

    products = Product.query.filter(Product.name.ilike(f'%{query}%')).limit(10).all()

    return jsonify([{
        'id': p.id,
        'name': p.name,
        'price': p.price,
        'category': p.category,
        'photo': p.photo_urls.split(',')[0] if p.photo_urls else '',
        'rating_avg': p.rating_avg
    } for p in products])

@app.route('/api/products', methods=['POST'])
@admin_required
def create_product():
    data = request.json
    name = data.get('name')
    category = data.get('category')
    price = data.get('price')
    sizes = data.get('sizes')
    colors = data.get('colors', [])
    description = data.get('description', '')
    brand = data.get('brand', 'KISIK')
    material = data.get('material', '')
    country = data.get('country', 'Україна')
    photo_urls = data.get('photo_urls', [])
    size_chart_url = data.get('size_chart_url')

    if not all([name, category, price, sizes]):
        return jsonify({'error': 'Заповніть всі обов\'язкові поля'}), 400

    product = Product(
        name=name, category=category, price=float(price), sizes=','.join(sizes),
        colors=','.join(colors), description=description, brand=brand,
        material=material, country=country, photo_urls=','.join(photo_urls),
        size_chart_url=size_chart_url
    )
    db.session.add(product)
    db.session.commit()
    return jsonify({'id': product.id})

@app.route('/api/products/<int:product_id>', methods=['PUT'])
@admin_required
def update_product(product_id):
    product = db.session.get(Product, product_id)
    if not product:
        return jsonify({'error': 'Товар не знайдено'}), 404
    data = request.json
    if 'name' in data:
        product.name = data['name']
    if 'category' in data:
        product.category = data['category']
    if 'price' in data:
        product.price = float(data['price'])
    if 'sizes' in data:
        product.sizes = ','.join(data['sizes'])
    if 'colors' in data:
        product.colors = ','.join(data['colors'])
    if 'description' in data:
        product.description = data['description']
    if 'brand' in data:
        product.brand = data['brand']
    if 'material' in data:
        product.material = data['material']
    if 'country' in data:
        product.country = data['country']
    if 'size_chart_url' in data:
        product.size_chart_url = data['size_chart_url']
    if 'photo_urls' in data:
        old_urls = product.photo_urls.split(',') if product.photo_urls else []
        for url in old_urls:
            if url not in data['photo_urls']:
                delete_photo_from_r2(url)
        product.photo_urls = ','.join(data['photo_urls'])
    db.session.commit()
    return jsonify({'status': 'ok'})

@app.route('/api/products/<int:product_id>', methods=['DELETE'])
@admin_required
def delete_product(product_id):
    product = db.session.get(Product, product_id)
    if not product:
        return jsonify({'error': 'Товар не знайдено'}), 404
    if product.photo_urls:
        for url in product.photo_urls.split(','):
            delete_photo_from_r2(url)
    if product.size_chart_url:
        delete_photo_from_r2(product.size_chart_url)
    db.session.delete(product)
    db.session.commit()
    return jsonify({'status': 'ok'})

@app.route('/api/upload-photo', methods=['POST'])
@admin_required
def upload_photo():
    if 'photo' not in request.files:
        return jsonify({'error': 'Немає фото'}), 400
    file = request.files['photo']
    if file.filename == '':
        return jsonify({'error': 'Файл не вибрано'}), 400
    file_data = file.read()
    if len(file_data) > 10 * 1024 * 1024:
        return jsonify({'error': 'Файл занадто великий'}), 400
    url = upload_photo_to_r2(file_data, file.filename)
    return jsonify({'url': url})

@app.route('/api/upload-size-chart', methods=['POST'])
@admin_required
def upload_size_chart():
    if 'photo' not in request.files:
        return jsonify({'error': 'Немає фото'}), 400
    file = request.files['photo']
    if file.filename == '':
        return jsonify({'error': 'Файл не вибрано'}), 400
    file_data = file.read()
    if len(file_data) > 10 * 1024 * 1024:
        return jsonify({'error': 'Файл занадто великий'}), 400
    url = upload_size_chart_to_r2(file_data, file.filename)
    return jsonify({'url': url})

# Кошик
@app.route('/api/cart', methods=['GET'])
def get_cart():
    session_id = get_cart_session_id()
    cart_items = CartItem.query.filter_by(session_id=session_id).all()
    result = []
    for item in cart_items:
        product = db.session.get(Product, item.product_id)
        if product:
            result.append({
                'id': item.id, 'product_id': product.id, 'name': product.name,
                'price': product.price, 'size': item.size, 'color': item.color,
                'quantity': item.quantity,
                'photo': product.photo_urls.split(',')[0] if product.photo_urls else ''
            })
    return jsonify(result)

@app.route('/api/cart', methods=['POST'])
def add_to_cart():
    session_id = get_cart_session_id()
    data = request.json
    product_id = data.get('product_id')
    size = data.get('size')
    color = data.get('color')
    quantity = data.get('quantity', 1)

    product = db.session.get(Product, product_id)
    if not product:
        return jsonify({'error': 'Товар не знайдено'}), 404
    if size not in product.sizes.split(','):
        return jsonify({'error': 'Розмір недоступний'}), 400

    existing = CartItem.query.filter_by(session_id=session_id, product_id=product_id, size=size, color=color).first()
    if existing:
        existing.quantity += quantity
    else:
        cart_item = CartItem(session_id=session_id, product_id=product_id, size=size, color=color, quantity=quantity)
        db.session.add(cart_item)
    db.session.commit()
    return jsonify({'status': 'ok'})

@app.route('/api/cart/<int:cart_item_id>', methods=['DELETE'])
def remove_from_cart(cart_item_id):
    session_id = get_cart_session_id()
    item = CartItem.query.filter_by(id=cart_item_id, session_id=session_id).first()
    if item:
        db.session.delete(item)
        db.session.commit()
    return jsonify({'status': 'ok'})

@app.route('/api/cart/clear', methods=['POST'])
def clear_cart():
    session_id = get_cart_session_id()
    CartItem.query.filter_by(session_id=session_id).delete()
    db.session.commit()
    return jsonify({'status': 'ok'})

@app.route('/api/cart/update', methods=['POST'])
def update_cart():
    session_id = get_cart_session_id()
    data = request.json
    cart_item_id = data.get('cart_item_id')
    quantity = data.get('quantity')
    item = CartItem.query.filter_by(id=cart_item_id, session_id=session_id).first()
    if item and quantity > 0:
        item.quantity = quantity
        db.session.commit()
    return jsonify({'status': 'ok'})

# Замовлення
@app.route('/api/checkout', methods=['POST'])
def checkout():
    data = request.json
    name = data.get('name')
    phone = data.get('phone')
    email = data.get('email')
    city = data.get('city')
    warehouse_address = data.get('warehouse_address', '')
    payment_method = data.get('payment_method', 'full')

    if not all([name, phone, city]):
        return jsonify({'error': 'Заповніть всі обов\'язкові поля'}), 400

    session_id = get_cart_session_id()
    cart_items = CartItem.query.filter_by(session_id=session_id).all()

    if not cart_items:
        return jsonify({'error': 'Кошик порожній'}), 400

    user_id = session.get('user_id')

    total_price = 0
    items_list = []
    for item in cart_items:
        product = db.session.get(Product, item.product_id)
        if product:
            item_total = product.price * item.quantity
            total_price += item_total
            items_list.append({
                'id': product.id, 'name': product.name, 'size': item.size,
                'color': item.color, 'quantity': item.quantity,
                'price': product.price, 'total': item_total
            })

    order = Order(
        order_number=generate_order_number(),
        user_id=user_id,
        guest_session=session_id if not user_id else None,
        user_name=name, user_phone=phone, user_email=email,
        city=city, warehouse_address=warehouse_address,
        payment_method=payment_method,
        selected_color=cart_items[0].color if cart_items else None,
        total_price=total_price,
        items=json.dumps(items_list, ensure_ascii=False)
    )
    db.session.add(order)
    db.session.flush()

    if user_id:
        user = db.session.get(User, user_id)
        if user:
            if phone:
                user.phone = phone
            if email:
                user.email = email

    CartItem.query.filter_by(session_id=session_id).delete()
    db.session.commit()

    thread = threading.Thread(target=send_order_notification, args=(order.id, items_list))
    thread.daemon = True
    thread.start()

    return jsonify({'status': 'ok', 'order_number': order.order_number})

@app.route('/api/orders', methods=['GET'])
@login_required
def get_orders():
    user = db.session.get(User, session['user_id'])
    orders = Order.query.filter_by(user_id=user.id).order_by(Order.created_at.desc()).all()
    return jsonify([{
        'id': o.id, 'order_number': o.order_number, 'total_price': o.total_price,
        'status': o.status, 'created_at': o.created_at.isoformat(),
        'items': json.loads(o.items) if o.items else []
    } for o in orders])

# Адмін API
@app.route('/api/admin/orders', methods=['GET'])
@admin_required
def admin_get_orders():
    orders = Order.query.order_by(Order.created_at.desc()).all()
    return jsonify([{
        'id': o.id, 'order_number': o.order_number, 'user_name': o.user_name,
        'user_phone': o.user_phone, 'user_email': o.user_email, 'city': o.city,
        'warehouse_address': o.warehouse_address, 'payment_method': o.payment_method,
        'selected_color': o.selected_color, 'total_price': o.total_price,
        'status': o.status, 'created_at': o.created_at.isoformat(),
        'items': json.loads(o.items) if o.items else [], 'guest': o.user_id is None
    } for o in orders])

@app.route('/api/admin/orders/<int:order_id>/status', methods=['PUT'])
@admin_required
def admin_update_order_status(order_id):
    order = db.session.get(Order, order_id)
    if not order:
        return jsonify({'error': 'Замовлення не знайдено'}), 404
    data = request.json
    order.status = data.get('status', order.status)
    db.session.commit()
    return jsonify({'status': 'ok'})

@app.route('/api/admin/orders/<int:order_id>', methods=['PUT'])
@admin_required
def admin_update_order(order_id):
    order = db.session.get(Order, order_id)
    if not order:
        return jsonify({'error': 'Замовлення не знайдено'}), 404
    
    data = request.json
    if 'user_name' in data:
        order.user_name = data['user_name']
    if 'user_phone' in data:
        order.user_phone = data['user_phone']
    if 'user_email' in data:
        order.user_email = data['user_email']
    if 'city' in data:
        order.city = data['city']
    if 'warehouse_address' in data:
        order.warehouse_address = data['warehouse_address']
    if 'status' in data:
        order.status = data['status']
    if 'payment_method' in data:
        order.payment_method = data['payment_method']
    
    db.session.commit()
    return jsonify({'status': 'ok'})

@app.route('/api/admin/orders/<int:order_id>', methods=['DELETE'])
@admin_required
def admin_delete_order(order_id):
    order = db.session.get(Order, order_id)
    if not order:
        return jsonify({'error': 'Замовлення не знайдено'}), 404
    db.session.delete(order)
    db.session.commit()
    return jsonify({'status': 'ok'})

@app.route('/api/admin/users', methods=['GET'])
@admin_required
def admin_get_users():
    users = User.query.all()
    return jsonify([{
        'id': u.id, 'username': u.username, 'is_admin': u.is_admin,
        'phone': u.phone, 'email': u.email, 'created_at': u.created_at.isoformat()
    } for u in users])

@app.route('/api/admin/users/<int:user_id>/role', methods=['PUT'])
@admin_required
def admin_set_user_role(user_id):
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({'error': 'Користувача не знайдено'}), 404
    data = request.json
    role = data.get('role')
    if role == 'admin':
        user.is_admin = True
    elif role == 'user':
        user.is_admin = False
    else:
        return jsonify({'error': 'Невірна роль'}), 400
    db.session.commit()
    return jsonify({'status': 'ok'})

@app.route('/api/admin/users/<int:user_id>', methods=['DELETE'])
@admin_required
def admin_delete_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({'error': 'Користувача не знайдено'}), 404
    if user.username == 'admin':
        return jsonify({'error': 'Не можна видалити головного адміністратора'}), 400
    
    Favorite.query.filter_by(user_id=user_id).delete()
    Review.query.filter_by(user_id=user_id).delete()
    Order.query.filter_by(user_id=user_id).update({'user_id': None})
    
    db.session.delete(user)
    db.session.commit()
    return jsonify({'status': 'ok'})

@app.route('/api/admin/reviews/<int:review_id>', methods=['PUT'])
@admin_required
def admin_update_review(review_id):
    review = db.session.get(Review, review_id)
    if not review:
        return jsonify({'error': 'Відгук не знайдено'}), 404
    
    data = request.json
    old_rating = review.rating
    new_rating = data.get('rating', old_rating)
    new_comment = data.get('comment', review.comment)
    
    if new_rating != old_rating:
        product = db.session.get(Product, review.product_id)
        if product:
            product.rating_sum = max(0, product.rating_sum - old_rating + new_rating)
    
    review.rating = new_rating
    review.comment = new_comment
    db.session.commit()
    return jsonify({'status': 'ok'})

@app.route('/api/admin/reviews/<int:review_id>', methods=['DELETE'])
@admin_required
def admin_delete_review(review_id):
    review = db.session.get(Review, review_id)
    if not review:
        return jsonify({'error': 'Відгук не знайдено'}), 404
    
    product = db.session.get(Product, review.product_id)
    if product:
        product.rating_sum = max(0, product.rating_sum - review.rating)
        product.rating_count = max(0, product.rating_count - 1)
    
    db.session.delete(review)
    db.session.commit()
    return jsonify({'status': 'ok'})

@app.route('/api/admin/stats', methods=['GET'])
@admin_required
def admin_get_stats():
    total_orders = Order.query.count()
    total_users = User.query.count()
    total_products = Product.query.count()
    total_revenue = db.session.query(func.sum(Order.total_price)).scalar() or 0
    new_orders = Order.query.filter_by(status='new').count()
    completed_orders = Order.query.filter_by(status='completed').count()
    return jsonify({
        'total_orders': total_orders, 'total_users': total_users,
        'total_products': total_products, 'total_revenue': float(total_revenue),
        'new_orders': new_orders, 'completed_orders': completed_orders
    })

@app.route('/api/admin/database', methods=['GET'])
@admin_required
def admin_get_database():
    categories = Category.query.all()
    colors = Color.query.all()
    users = User.query.all()
    products = Product.query.all()
    orders = Order.query.order_by(Order.created_at.desc()).all()
    cart_items = CartItem.query.all()
    reviews = Review.query.all()
    favorites = Favorite.query.all()
    return jsonify({
        'categories': [{'id': c.id, 'name': c.name, 'icon': c.icon} for c in categories],
        'colors': [{'id': c.id, 'name': c.name, 'code': c.code, 'icon': c.icon} for c in colors],
        'users': [{'id': u.id, 'username': u.username, 'is_admin': u.is_admin, 'phone': u.phone, 'email': u.email} for u in users],
        'products': [{'id': p.id, 'name': p.name, 'category': p.category, 'price': p.price, 'brand': p.brand, 'material': p.material, 'size_chart_url': p.size_chart_url, 'rating_avg': p.rating_avg} for p in products],
        'orders': [{'id': o.id, 'order_number': o.order_number, 'user_name': o.user_name, 'total_price': o.total_price, 'status': o.status} for o in orders],
        'cart_items': [{'id': c.id, 'product_id': c.product_id, 'size': c.size, 'color': c.color, 'quantity': c.quantity} for c in cart_items],
        'reviews': [{'id': r.id, 'product_id': r.product_id, 'user_name': r.user_name, 'rating': r.rating, 'comment': r.comment, 'created_at': r.created_at.isoformat()} for r in reviews],
        'favorites': [{'id': f.id, 'user_id': f.user_id, 'product_id': f.product_id} for f in favorites],
        'statistics': {
            'total_categories': len(categories), 'total_colors': len(colors),
            'total_users': len(users), 'total_products': len(products),
            'total_orders': len(orders), 'total_cart_items': len(cart_items),
            'total_reviews': len(reviews), 'total_favorites': len(favorites)
        }
    })

# Telegram конфіг
@app.route('/api/telegram-config', methods=['GET'])
def get_telegram_config():
    return jsonify({
        'support_chat_id': TELEGRAM_SUPPORT_CHAT_ID,
        'bot_link': TELEGRAM_BOT_LINK
    })

# Тестовий маршрут
@app.route('/api/test-telegram', methods=['GET'])
def test_telegram():
    results = {}
    for chat_id in ADMIN_CHAT_IDS:
        success = send_telegram_message(f"🔄 Тестове повідомлення від KISIK MARKET\nЧас: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", chat_id)
        results[str(chat_id)] = 'sent' if success else 'failed'
    return jsonify({'status': 'ok', 'results': results})

# Сторінки
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/product/<int:product_id>')
def product_page(product_id):
    return render_template('product.html', product_id=product_id)

@app.route('/admin')
def admin_panel():
    if is_admin():
        return render_template('admin.html')
    return render_template('admin_login.html')

# Ініціалізація бази даних
def init_db():
    """Створює таблиці та додає початкові дані"""
    db.create_all()
    migrate_database()

    # Default categories
    default_categories = [('Худі', '👕'), ('Штани', '👖'), ('Футболки', '👕'), ('Шорти', '🩳'), ('Кросівки', '👟')]
    for cat_name, cat_icon in default_categories:
        if not Category.query.filter_by(name=cat_name).first():
            db.session.add(Category(name=cat_name, icon=cat_icon))

    # Default colors
    default_colors = [
        ('Чорний', '#000000', '⚫'), ('Білий', '#FFFFFF', '⚪'), ('Сірий', '#808080', '◻️'),
        ('Синій', '#0000FF', '🔵'), ('Червоний', '#FF0000', '🔴'), ('Зелений', '#00FF00', '🟢'),
        ('Жовтий', '#FFFF00', '🟡'), ('Фіолетовий', '#800080', '🟣'), ('Рожевий', '#FFC0CB', '🌸'),
        ('Бежевий', '#F5F5DC', '📦')
    ]
    for color_name, color_code, color_icon in default_colors:
        if not Color.query.filter_by(name=color_name).first():
            db.session.add(Color(name=color_name, code=color_code, icon=color_icon))

    # Admin user
    if not User.query.filter_by(username='admin').first():
        admin = User(username='admin', password=hash_password('admin123'), is_admin=True)
        db.session.add(admin)
        logger.info("✅ Адмін створений: admin / admin123")

    db.session.commit()

    # Test products if none exist
    if Product.query.count() == 0:
        products = [
            Product(name='Худі KISIK Black', category='Худі', price=1299, sizes='S,M,L,XL', colors='Чорний',
                   description="Стильне худі з бавовни преміум-класу.", brand='KISIK', material='100% бавовна',
                   country='Україна', photo_urls='https://images.unsplash.com/photo-1556821840-3a63f95609a7?w=800'),
            Product(name='Худі KISIK White', category='Худі', price=1299, sizes='S,M,L,XL', colors='Білий',
                   description='Елегантне біле худі з якісного матеріалу.', brand='KISIK', material='95% бавовна, 5% еластан',
                   country='Україна', photo_urls='https://images.unsplash.com/photo-1556905055-8f358a7a47b2?w=800'),
            Product(name='Футболка KISIK Logo', category='Футболки', price=599, sizes='XS,S,M,L,XL,XXL', colors='Чорний,Білий,Сірий',
                   description='Класична футболка з принтом.', brand='KISIK', material='100% бавовна',
                   country='Україна', photo_urls='https://images.unsplash.com/photo-1521572163474-6864f9cf17ab?w=800'),
            Product(name='Кросівки KISIK Sport', category='Кросівки', price=2499, sizes='36,37,38,39,40,41,42,43,44,45', colors='Чорний,Білий,Сірий',
                   description='Спортивні кросівки для активного відпочинку.', brand='KISIK', material='Текстиль, шкіра',
                   country='Україна', photo_urls='https://images.unsplash.com/photo-1542291026-7eec264c27ff?w=800'),
            Product(name='Кросівки KISIK Run', category='Кросівки', price=2999, sizes='36,37,38,39,40,41,42,43,44,45', colors='Синій,Червоний',
                   description='Бігові кросівки з покращеною амортизацією.', brand='KISIK', material='Текстиль, синтетика',
                   country='Україна', photo_urls='https://images.unsplash.com/photo-1608231387042-66d1773070a5?w=800'),
        ]
        for p in products:
            db.session.add(p)
        db.session.commit()
        logger.info("✅ Тестові товари додані")

    logger.info("📊 БАЗА ДАНИХ ГОТОВА!")

# ============ ЗАПУСК ============
if __name__ == '__main__':
    with app.app_context():
        init_db()

    print("\n" + "=" * 60)
    print("🛍️ KISIK MARKET - МАГАЗИН ЗАПУЩЕНО!")
    print("=" * 60)
    print("📱 ГОЛОВНА СТОРІНКА: http://localhost:5000")
    print("👑 АДМІН ПАНЕЛЬ: http://localhost:5000/admin")
    print("")
    print("🔐 ДОСТУП ДО АДМІНКИ:")
    print("   Логін: admin")
    print("   Пароль: admin123")
    print("=" * 60)

    app.run(debug=True, host='0.0.0.0', port=5000, threaded=True)
