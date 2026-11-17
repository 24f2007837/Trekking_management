import os
import uuid
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_security import Security, SQLAlchemyUserDatastore, UserMixin, RoleMixin, current_user
from flask_security.utils import hash_password, verify_password
import json
from celery import Celery

app = Flask(__name__)
app.config['SECRET_KEY'] = 'super-secret-trek-key'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///trek.db'
app.config['SECURITY_PASSWORD_SALT'] = 'super-salt-key'
app.config['WTF_CSRF_ENABLED'] = False
app.config['SECURITY_TOKEN_AUTHENTICATION_HEADER'] = 'Authentication-Token'

app.config['broker_url'] = 'memory://'
app.config['result_backend'] = 'db+sqlite:///celery_results.db'

db = SQLAlchemy(app)

import redis
try:
    redis_cache = redis.Redis(host='localhost', port=6379, db=1, decode_responses=True)
    redis_cache.ping()
    REDIS_AVAILABLE = True
except Exception:
    redis_cache = None
    REDIS_AVAILABLE = False

celery = Celery(app.name, broker=app.config['broker_url'])
celery.conf.update(app.config)

roles_users = db.Table('roles_users',
    db.Column('user_id', db.Integer(), db.ForeignKey('user.id')),
    db.Column('role_id', db.Integer(), db.ForeignKey('role.id'))
)

class Role(db.Model, RoleMixin):
    id = db.Column(db.Integer(), primary_key=True)
    name = db.Column(db.String(80), unique=True)
    description = db.Column(db.String(255))

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    contact = db.Column(db.String(50), nullable=True)
    active = db.Column(db.Boolean(), default=True)
    fs_uniquifier = db.Column(db.String(255), unique=True, nullable=False)
    roles = db.relationship('Role', secondary=roles_users, backref=db.backref('users', lazy='dynamic'))

class Trek(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    location = db.Column(db.String(100), nullable=False)
    difficulty = db.Column(db.String(50), nullable=False)
    duration = db.Column(db.Integer, nullable=False)
    available_slots = db.Column(db.Integer, nullable=False)
    start_date = db.Column(db.String(50), nullable=False)
    end_date = db.Column(db.String(50), nullable=False)
    status = db.Column(db.String(50), default='Open')
    staff_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    staff = db.relationship('User', foreign_keys=[staff_id], backref='assigned_treks')

class Booking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    trek_id = db.Column(db.Integer, db.ForeignKey('trek.id'), nullable=False)
    booking_date = db.Column(db.String(50), default=lambda: datetime.utcnow().strftime('%Y-%m-%d'))
    status = db.Column(db.String(50), default='Booked')
    payment_status = db.Column(db.String(50), default='Paid')
    user = db.relationship('User', backref='bookings')
    trek = db.relationship('Trek', backref='bookings')

user_datastore = SQLAlchemyUserDatastore(db, User, Role)
security = Security(app, user_datastore)

with app.app_context():
    db.create_all()
    user_datastore.find_or_create_role(name='admin', description='Administrator')
    user_datastore.find_or_create_role(name='staff', description='Trek Staff')
    user_datastore.find_or_create_role(name='user', description='Trekker')

    admin_user = User.query.filter_by(email='admin@trek.com').first()
    if not admin_user:
     
        admin_user = user_datastore.create_user(
        email='admin@trek.com', 
        password=hash_password('admin123'), 
        name='System Admin', 
        contact='9876543210', 
        active=True,
        fs_uniquifier='admin-unique-token-123'
    )
    user_datastore.add_role_to_user(admin_user, 'admin')
    db.session.commit()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json()
    user = user_datastore.find_user(email=data.get('email'))
    if user and verify_password(data.get('password'), user.password):
        if not user.active:
            return jsonify({'error': 'Account is deactivated or blacklisted.'}), 403
        return jsonify({
            'token': user.get_auth_token(),
            'email': user.email,
            'name': user.name,
            'contact': user.contact,
            'roles': [r.name for r in user.roles]
        })
    return jsonify({'error': 'Invalid credentials'}), 401

@app.route('/api/register', methods=['POST'])
def api_register():
    data = request.get_json()
    if user_datastore.find_user(email=data.get('email')):
        return jsonify({'error': 'Email already registered'}), 400
    user = user_datastore.create_user(
        email=data.get('email'),
        password=hash_password(data.get('password')),
        name=data.get('name'),
        contact=data.get('contact', ''),
        active=True,
        fs_uniquifier=str(uuid.uuid4())
    )
    user_datastore.add_role_to_user(user, 'user')
    db.session.commit()
    return jsonify({'message': 'Registration successful'})

@app.route('/api/treks', methods=['GET'])
def get_treks():
    query = Trek.query
    if request.args.get('location'):
        query = query.filter(Trek.location.ilike(f"%{request.args.get('location')}%"))
    if request.args.get('difficulty'):
        query = query.filter_by(difficulty=request.args.get('difficulty'))
    if request.args.get('duration'):
        query = query.filter_by(duration=int(request.args.get('duration')))
    if request.args.get('search'):
        s = request.args.get('search')
        query = query.filter(Trek.name.ilike(f'%{s}%') | Trek.location.ilike(f'%{s}%'))

    treks = [{
        'id': t.id, 'name': t.name, 'location': t.location, 'difficulty': t.difficulty,
        'duration': t.duration, 'available_slots': t.available_slots, 'start_date': t.start_date,
        'end_date': t.end_date, 'status': t.status, 'staff_id': t.staff_id,
        'staff_name': t.staff.name if t.staff else 'Unassigned'
    } for t in query.all()]
    return jsonify(treks)

@app.route('/api/admin/treks', methods=['GET', 'POST'])
def admin_treks():
    if request.method == 'GET': 
        return get_treks()
    
    if not current_user.is_authenticated or not current_user.has_role('admin'):
        return jsonify({'error': 'Unauthorized'}), 403

    data = request.get_json()
    try:
        trek = Trek(
            name=data.get('name'), 
            location=data.get('location'), 
            difficulty=data.get('difficulty'),
            duration=int(data.get('duration', 1)), 
            available_slots=int(data.get('available_slots', 10)),
            start_date=data.get('start_date'), 
            end_date=data.get('end_date'),
            status='Open', 
            staff_id=int(data.get('staff_id')) if data.get('staff_id') else None
        )
        db.session.add(trek)
        db.session.commit()
        if REDIS_AVAILABLE and redis_cache:
            try: redis_cache.flushdb()
            except Exception: pass
        return jsonify({'message': 'Trek created successfully'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/admin/trek/<int:trek_id>', methods=['PUT', 'DELETE'])
def admin_manage_trek(trek_id):
    if not current_user.is_authenticated or not current_user.has_role('admin'):
        return jsonify({'error': 'Unauthorized'}), 403
    
    trek = Trek.query.get_or_404(trek_id)
    
    if request.method == 'DELETE':
        db.session.delete(trek)
        db.session.commit()
        if REDIS_AVAILABLE and redis_cache:
            try: redis_cache.flushdb()
            except Exception: pass
        return jsonify({'message': 'Trek deleted successfully'})
    
    data = request.get_json()
    try:
        trek.name = data.get('name', trek.name)
        trek.location = data.get('location', trek.location)
        trek.difficulty = data.get('difficulty', trek.difficulty)
        trek.duration = int(data.get('duration', trek.duration))
        trek.available_slots = int(data.get('available_slots', trek.available_slots))
        trek.start_date = data.get('start_date', trek.start_date)
        trek.end_date = data.get('end_date', trek.end_date)
        trek.status = data.get('status', trek.status)
        trek.staff_id = int(data.get('staff_id')) if data.get('staff_id') else None
        
        db.session.commit()
        if REDIS_AVAILABLE and redis_cache:
            try: redis_cache.flushdb()
            except Exception: pass
        return jsonify({'message': 'Trek updated successfully'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/admin/staff', methods=['GET', 'POST'])
def admin_staff():
    if not current_user.is_authenticated or not current_user.has_role('admin'):
        return jsonify({'error': 'Unauthorized'}), 403

    if request.method == 'GET':
        staff_role = Role.query.filter_by(name='staff').first()
        return jsonify([{'id': s.id, 'name': s.name, 'email': s.email, 'contact': s.contact, 'active': s.active} for s in staff_role.users])
    
    data = request.get_json()
    if user_datastore.find_user(email=data.get('email')): 
        return jsonify({'error': 'Email exists'}), 400
    
    try:
        staff = user_datastore.create_user(
            email=data.get('email'), password=hash_password(data.get('password')),
            name=data.get('name'), contact=data.get('contact', ''), active=True, fs_uniquifier=str(uuid.uuid4())
        )
        user_datastore.add_role_to_user(staff, 'staff')
        db.session.commit()
        return jsonify({'message': 'Staff created successfully'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/admin/users', methods=['GET'])
def admin_users():
    if not current_user.is_authenticated or not current_user.has_role('admin'):
        return jsonify({'error': 'Unauthorized'}), 403
    user_role = Role.query.filter_by(name='user').first()
    return jsonify([{'id': u.id, 'name': u.name, 'email': u.email, 'contact': u.contact, 'active': u.active} for u in user_role.users])

@app.route('/api/admin/user/<int:user_id>/toggle', methods=['PUT'])
def admin_toggle_user(user_id):
    if not current_user.is_authenticated or not current_user.has_role('admin'):
        return jsonify({'error': 'Unauthorized'}), 403
    user = User.query.get_or_404(user_id)
    user.active = not user.active
    db.session.commit()
    return jsonify({'message': f'Account status updated to {"Active" if user.active else "Blacklisted"}'})

@app.route('/api/admin/stats', methods=['GET'])
def admin_stats():
    if not current_user.is_authenticated or not current_user.has_role('admin'):
        return jsonify({'error': 'Unauthorized'}), 403
    
    total_treks = Trek.query.count()
    total_bookings = Booking.query.count()
    user_role = Role.query.filter_by(name='user').first()
    total_users = user_role.users.count() if user_role else 0
    staff_role = Role.query.filter_by(name='staff').first()
    total_staff = staff_role.users.count() if staff_role else 0

    trek_stats = []
    for t in Trek.query.all():
        b_count = Booking.query.filter_by(trek_id=t.id).count()
        trek_stats.append({
            'name': t.name,
            'location': t.location,
            'status': t.status,
            'bookings_count': b_count,
            'available_slots': t.available_slots
        })

    return jsonify({
        'total_treks': total_treks,
        'total_bookings': total_bookings,
        'total_users': total_users,
        'total_staff': total_staff,
        'trek_stats': trek_stats
    })

@app.route('/api/user/book', methods=['POST'])
def book_trek():
    if not current_user.is_authenticated or not current_user.has_role('user'):
        return jsonify({'error': 'Only trekkers can book treks'}), 403
    data = request.get_json()
    trek = Trek.query.get_or_404(data.get('trek_id'))
    
    if trek.available_slots <= 0 or trek.status != 'Open':
        return jsonify({'error': 'Trek is not Open or fully booked'}), 400

    existing = Booking.query.filter_by(user_id=current_user.id, trek_id=trek.id, status='Booked').first()
    if existing:
        return jsonify({'error': 'You have already booked this trek'}), 400

    booking = Booking(user_id=current_user.id, trek_id=trek.id, payment_status='Paid')
    trek.available_slots -= 1
    db.session.add(booking)
    db.session.commit()
    return jsonify({'message': 'Trek booked successfully'})

@app.route('/api/user/history', methods=['GET'])
def user_history():
    if not current_user.is_authenticated:
        return jsonify({'error': 'Unauthorized'}), 403
    bookings = Booking.query.filter_by(user_id=current_user.id).all()
    return jsonify([{
        'booking_id': b.id, 'trek_name': b.trek.name, 'location': b.trek.location,
        'start_date': b.trek.start_date, 'status': b.status, 'payment_status': b.payment_status
    } for b in bookings])

@app.route('/api/staff/treks', methods=['GET'])
def staff_treks():
    if not current_user.is_authenticated or (not current_user.has_role('staff') and not current_user.has_role('admin')):
        return jsonify({'error': 'Unauthorized'}), 403
    treks = Trek.query.filter_by(staff_id=current_user.id).all()
    return jsonify([{
        'id': t.id, 'name': t.name, 'location': t.location, 'difficulty': t.difficulty,
        'duration': t.duration, 'available_slots': t.available_slots, 'status': t.status
    } for t in treks])

@app.route('/api/staff/trek/<int:trek_id>', methods=['PUT'])
def staff_update_trek(trek_id):
    if not current_user.is_authenticated or (not current_user.has_role('staff') and not current_user.has_role('admin')):
        return jsonify({'error': 'Unauthorized'}), 403
    trek = Trek.query.get_or_404(trek_id)
    if trek.staff_id != current_user.id and not current_user.has_role('admin'):
        return jsonify({'error': 'Unauthorized'}), 403
    
    data = request.get_json()
    if 'available_slots' in data: trek.available_slots = int(data['available_slots'])
    if 'status' in data: trek.status = data['status']
    db.session.commit()
    return jsonify({'message': 'Trek updated successfully'})

if __name__ == '__main__':
    app.run(debug=True, port=5000)