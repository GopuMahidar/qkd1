from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
import uuid
from datetime import datetime
import logging
import threading
import time
import hashlib
import secrets
import string

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-super-secure-secret-key-change-this'

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='threading',
    ping_timeout=60,
    ping_interval=25
)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Connection Manager
class ConnectionManager:
    def __init__(self):
        self.connections = {}
        self.message_queue = {}
        self.lock = threading.Lock()

    def add_connection(self, user_type, sid, ip_address):
        with self.lock:
            self.connections[user_type] = {
                'sid': sid,
                'ip': ip_address,
                'connected_at': datetime.now(),
                'last_seen': datetime.now(),
                'status': 'online'
            }
            if user_type not in self.message_queue:
                self.message_queue[user_type] = []
            logger.info(f"User {user_type} connected from {ip_address} with SID {sid}")

    def remove_connection(self, sid):
        with self.lock:
            for user_type, conn_data in list(self.connections.items()):
                if conn_data and conn_data['sid'] == sid:
                    self.connections[user_type] = None
                    logger.info(f"User {user_type} disconnected")
                    break

    def get_target_connection(self, sender_type):
        target_type = 'receiver' if sender_type == 'sender' else 'sender'
        return self.connections.get(target_type)

    def update_last_seen(self, user_type):
        with self.lock:
            if user_type in self.connections and self.connections[user_type]:
                self.connections[user_type]['last_seen'] = datetime.now()

    def queue_message(self, target_user, message):
        with self.lock:
            if target_user not in self.message_queue:
                self.message_queue[target_user] = []
            self.message_queue[target_user].append(message)

    def get_queued_messages(self, user_type):
        with self.lock:
            messages = self.message_queue.get(user_type, [])
            self.message_queue[user_type] = []
            return messages


# Global objects
conn_manager = ConnectionManager()
messages_store = []
session_keys = {}

# Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/sender')
def sender_panel():
    return render_template('sender.html')

@app.route('/receiver')
def receiver_panel():
    return render_template('receiver.html')

@app.route('/api/status')
def api_status():
    with conn_manager.lock:
        return jsonify({
            'server_status': 'online',
            'connections': {
                'sender': conn_manager.connections.get('sender') is not None,
                'receiver': conn_manager.connections.get('receiver') is not None
            },
            'total_messages': len(messages_store),
            'server_time': datetime.now().isoformat()
        })

# Socket.IO events
@socketio.on('connect')
def handle_connect(auth=None):
    client_ip = request.environ.get('HTTP_X_REAL_IP', request.environ.get('REMOTE_ADDR'))
    logger.info(f'Client connected from {client_ip} with SID: {request.sid}')
    emit('connection_established', {
        'status': 'connected',
        'server_time': datetime.now().isoformat(),
        'your_ip': client_ip,
        'session_id': request.sid
    })

@socketio.on('disconnect')
def handle_disconnect():
    logger.info(f'Client disconnected: {request.sid}')
    conn_manager.remove_connection(request.sid)
    emit('user_disconnected', {'timestamp': datetime.now().isoformat()}, broadcast=True)

@socketio.on('join_as_user')
def handle_join(data):
    try:
        user_type = data['user_type']
        client_ip = request.environ.get('HTTP_X_REAL_IP', request.environ.get('REMOTE_ADDR'))

        conn_manager.add_connection(user_type, request.sid, client_ip)
        join_room(f'user_{user_type}')

        emit('user_joined', {
            'user_type': user_type,
            'ip_address': client_ip,
            'timestamp': datetime.now().isoformat()
        })

        target_conn = conn_manager.get_target_connection(user_type)
        if target_conn:
            socketio.emit('peer_connected', {
                'user_type': user_type,
                'ip_address': client_ip,
                'timestamp': datetime.now().isoformat()
            }, room=target_conn['sid'])

        queued_messages = conn_manager.get_queued_messages(user_type)
        for message in queued_messages:
            emit('receive_message', message)

    except Exception as e:
        logger.error(f'Error in join_as_user: {str(e)}')
        emit('error', {'message': 'Failed to join as user'})

@socketio.on('send_message')
def handle_message(data):
    try:
        sender_type = data['sender']
        message_text = data['message']
        encrypted_text = data.get('encrypted_text', '')

        message_data = {
            'id': str(uuid.uuid4()),
            'text': message_text,
            'encrypted_text': encrypted_text,
            'sender': sender_type,
            'timestamp': datetime.now().strftime('%H:%M:%S'),
            'date': datetime.now().strftime('%Y-%m-%d'),
            'status': 'pending',
            'sender_ip': conn_manager.connections.get(sender_type, {}).get('ip', 'unknown'),
            'checksum': hashlib.md5(message_text.encode()).hexdigest()
        }

        messages_store.append(message_data)
        conn_manager.update_last_seen(sender_type)

        target_conn = conn_manager.get_target_connection(sender_type)
        if target_conn and target_conn['status'] == 'online':
            socketio.emit('receive_message', message_data, room=target_conn['sid'])
            message_data['status'] = 'delivered'
            emit('message_delivered', {
                'message_id': message_data['id'],
                'delivered_at': datetime.now().isoformat(),
                'recipient_ip': target_conn['ip']
            })
        else:
            target_user = 'receiver' if sender_type == 'sender' else 'sender'
            conn_manager.queue_message(target_user, message_data)
            message_data['status'] = 'queued'
            emit('message_queued', {
                'message_id': message_data['id'],
                'reason': 'recipient_offline'
            })

        emit('message_sent', message_data)
        logger.info(f'Message from {sender_type}: {message_text[:50]}...')

    except Exception as e:
        logger.error(f'Error in send_message: {str(e)}')
        emit('error', {'message': 'Failed to send message'})

@socketio.on('generate_key')
def handle_generate_key():
    try:
        key = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))
        key_id = str(uuid.uuid4())
        session_keys[key_id] = {
            'key': key,
            'created_at': datetime.now(),
            'used_by': []
        }
        emit('key_generated', {
            'key': key,
            'key_id': key_id,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f'Error generating key: {str(e)}')
        emit('error', {'message': 'Key generation failed'})

# Background task for monitoring
def connection_monitor():
    while True:
        try:
            current_time = datetime.now()
            with conn_manager.lock:
                for user_type, conn_data in list(conn_manager.connections.items()):
                    if conn_data and conn_data['last_seen']:
                        time_diff = (current_time - conn_data['last_seen']).seconds
                        if time_diff > 120:
                            logger.warning(f'Connection timeout for {user_type}')
                            conn_data['status'] = 'timeout'
                            target_conn = conn_manager.get_target_connection(user_type)
                            if target_conn:
                                socketio.emit('peer_timeout', {
                                    'user_type': user_type,
                                    'timestamp': current_time.isoformat()
                                }, room=target_conn['sid'])
            time.sleep(30)
        except Exception as e:
            logger.error(f'Error in connection monitor: {str(e)}')
            time.sleep(30)

monitor_thread = threading.Thread(target=connection_monitor, daemon=True)
monitor_thread.start()

if __name__ == '__main__':
    logger.info("Starting SecureChat Server...")
    socketio.run(
        app,
        debug=True,
        host='0.0.0.0',
        port=5000,
        allow_unsafe_werkzeug=True
    )
