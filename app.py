import os, time, queue, threading, subprocess, random, json, shutil, math
import numpy as np
import cv2, librosa, imageio
import datetime as dt
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import secrets
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_from_directory, session
import requests

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

# ==========================================
# 🛡️ SETUP & MONITORING
# ==========================================
def auto_setup_dependencies():
    ffmpeg_found = shutil.which("ffmpeg") or os.path.exists("/usr/bin/ffmpeg")
    if not ffmpeg_found:
        print("⚙️ KEIBOT: ffmpeg tidak ditemukan, mencoba install otomatis...")
        ret = os.system("apt-get update -qq && apt-get install -y ffmpeg")
        if ret == 0: print("✅ ffmpeg berhasil diinstall!")
        else: print("❌ Gagal install ffmpeg otomatis. Jalankan manual: apt-get install -y ffmpeg")
    else:
        path = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
        print(f"✅ ffmpeg ditemukan: {path}")

auto_setup_dependencies()

last_cpu_idle = 0
last_cpu_total = 0

def get_system_stats():
    global last_cpu_idle, last_cpu_total
    cpu_pct = 0.0
    try:
        with open('/proc/stat', 'r') as f:
            parts = [int(i) for i in f.readline().split()[1:8]]
        idle = parts[3] + parts[4]
        total = sum(parts)
        if last_cpu_total > 0:
            diff_idle = idle - last_cpu_idle
            diff_total = total - last_cpu_total
            if diff_total > 0:
                cpu_pct = round(100.0 * (1.0 - diff_idle / diff_total), 1)
        last_cpu_idle = idle
        last_cpu_total = total
        if cpu_pct < 0.0: cpu_pct = 0.0
        if cpu_pct > 100.0: cpu_pct = 100.0
    except: pass

    try:
        import psutil
        cpu_pct = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        return {"cpu": cpu_pct, "ram_pct": mem.percent, "ram_used": round(mem.used / (1024**3), 2), "ram_total": round(mem.total / (1024**3), 2)}
    except: pass

    return {"cpu": cpu_pct, "ram_pct": 0.0, "ram_used": 0.0, "ram_total": 0.0}

# ==========================================
# 💾 DATABASE & FOLDER SYSTEM
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, 'static'))
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')

def is_configured(): return os.path.exists(CONFIG_FILE)
def load_bot_config():
    if is_configured():
        with open(CONFIG_FILE, 'r') as f: return json.load(f)
    return {}

bot_config = load_bot_config()
app.secret_key = bot_config.get('secret_key', secrets.token_hex(24))

@app.before_request
def check_security():
    allowed_routes = ['login', 'setup', 'static', 'serve_uploads', 'device_login', 'poll_device_token']
    if request.endpoint in allowed_routes: return
    if not is_configured(): return redirect(url_for('setup'))
    if 'logged_in' not in session: return redirect(url_for('login'))

@app.route('/setup', methods=['GET', 'POST'])
def setup():
    if is_configured(): return redirect(url_for('login'))
    error = None
    if request.method == 'POST':
        pin = request.form.get('new_pin'); pin2 = request.form.get('confirm_pin')
        if not pin or len(pin) < 3: error = "PIN minimal 3 karakter."
        elif pin != pin2: error = "PIN tidak cocok!"
        else:
            new_secret = secrets.token_hex(24)
            with open(CONFIG_FILE, 'w') as f: json.dump({"admin_pin": pin, "secret_key": new_secret}, f, indent=4)
            app.secret_key = new_secret; session['logged_in'] = True
            return redirect(url_for('index'))
    return render_template('setup.html', error=error)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if not is_configured(): return redirect(url_for('setup'))
    error = None
    if request.method == 'POST':
        if request.form.get('password') == load_bot_config().get('admin_pin'):
            session['logged_in'] = True; return redirect(url_for('index'))
        else: error = 'Akses Ditolak! PIN Salah.'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.pop('logged_in', None); return redirect(url_for('login'))

BASE_UPLOAD = os.path.join(BASE_DIR, "uploads")
DB_FILE = os.path.join(BASE_DIR, 'channels_db.json')
TASKS_FILE = os.path.join(BASE_DIR, 'tasks_db.json')
PRESETS_FILE = os.path.join(BASE_DIR, 'presets.json')
CLIENT_SECRETS_FILE = os.path.join(BASE_DIR, 'client_secret.json')
SCOPES = ['https://www.googleapis.com/auth/youtube', 'https://www.googleapis.com/auth/youtube.upload']

os.makedirs(BASE_UPLOAD, exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, 'static'), exist_ok=True)

db_lock = threading.Lock()

GALLERY_FOLDER_MAP = {
    'audio':      'audios',
    'audios':     'audios',
    'background': 'backgrounds',
    'backgrounds':'backgrounds',
    'thumbnail':  'thumbnails',
    'thumbnails': 'thumbnails'
}

def resolve_folder(g_type: str) -> str:
    return GALLERY_FOLDER_MAP.get(str(g_type).strip().lower(), 'audios')

def load_tasks_db():
    if os.path.exists(TASKS_FILE):
        try:
            with open(TASKS_FILE, 'r') as f: return json.load(f)
        except: return {"active": [], "history": []}
    return {"active": [], "history": []}

def save_tasks_db():
    with db_lock:
        data = {"active": active_tasks, "history": history_tasks}
        with open(TASKS_FILE, 'w') as f: json.dump(data, f, indent=4)

def load_channels():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r') as f: return json.load(f)
        except: return []
    return []

def save_channels(channels):
    with db_lock:
        with open(DB_FILE, 'w') as f: json.dump(channels, f, indent=4)

task_data = load_tasks_db()
active_tasks = task_data.get("active", [])
history_tasks = task_data.get("history", [])
database_channel = load_channels()

render_queue = queue.Queue()
stop_flags = {}
channel_cooldowns = {}

# 🎵 Cache audio info (durasi) — expire setelah 5 menit
_audio_info_cache = {}
AUDIO_CACHE_TTL = 300

# 🔥 SISTEM NOTIFIKASI LONCENG 🔥
system_notifications = []

def get_ffmpeg_path():
    local_exe = os.path.join(BASE_DIR, "ffmpeg.exe")
    if os.path.exists(local_exe): return local_exe
    found = shutil.which("ffmpeg")
    if found: return found
    for p in ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/bin/ffmpeg"]:
        if os.path.exists(p): return p
    raise FileNotFoundError("ffmpeg tidak ditemukan! Jalankan: apt-get install -y ffmpeg")

def get_ffprobe_path():
    found = shutil.which("ffprobe")
    if found: return found
    for p in ["/usr/bin/ffprobe", "/usr/local/bin/ffprobe", "/bin/ffprobe"]:
        if os.path.exists(p): return p
    return "ffprobe"

def wait_for_resources(task_id, max_ram_pct=85.0):
    while True:
        if stop_flags.get(task_id): return False
        stats = get_system_stats()
        if stats['ram_pct'] < max_ram_pct: return True
        with db_lock:
            for d in active_tasks:
                if d['id'] == task_id: d['status'] = f"Menunggu RAM Turun ({stats['ram_pct']}%) ⏳"
        save_tasks_db()
        time.sleep(10)

def move_to_history(task_id, final_status):
    global active_tasks, history_tasks
    with db_lock:
        for t in active_tasks:
            if t['id'] == task_id:
                t['status'] = final_status
                history_tasks.insert(0, t)
                active_tasks.remove(t)
                if len(history_tasks) > 50: history_tasks.pop()
                break
    save_tasks_db()

def get_fresh_credentials(channel_data):
    creds_str = channel_data.get('creds_list', [channel_data.get('creds_json')])[0]
    creds = Credentials.from_authorized_user_info(json.loads(creds_str))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds

# ==========================================
# 🚨 SATPAM API KEY (AUTO-CHECKER)
# ==========================================
def api_key_checker_worker():
    global system_notifications, database_channel
    while True:
        time.sleep(10) 
        new_notifs = []
        for c in database_channel:
            creds_list = c.get('creds_list', [c.get('creds_json', '')])
            for idx, cred_str in enumerate(creds_list):
                if not cred_str: continue
                try:
                    creds = Credentials.from_authorized_user_info(json.loads(cred_str))
                    if creds.expired and creds.refresh_token:
                        creds.refresh(Request())
                except Exception as e:
                    msg = f"⚠️ API Key #{idx+1} untuk Channel '{c.get('name','Unknown')}' EXPIRED! Silakan hapus dan tautkan ulang."
                    if not any(n['msg'] == msg for n in system_notifications):
                        new_notifs.append({"msg": msg, "time": datetime.now().strftime("%Y-%m-%d %H:%M")})
        
        if new_notifs:
            with db_lock:
                system_notifications.extend(new_notifs)
                
        time.sleep(43200)

threading.Thread(target=api_key_checker_worker, daemon=True).start()

# ==========================================
# 🏭 GALLERY & ASSET MANAGER
# ==========================================
def get_channel_folder(yt_id, sub):
    path = os.path.join(BASE_UPLOAD, yt_id, sub)
    os.makedirs(path, exist_ok=True)
    return path

def get_multi_backgrounds(yt_id, count=1):
    path = get_channel_folder(yt_id, "backgrounds")
    files = [os.path.join(path, f) for f in os.listdir(path) if f.lower().endswith(('.mp4', '.jpg', '.jpeg', '.png', '.gif', '.webp', '.mov'))]
    if not files: return []
    random.shuffle(files)
    
    selected = []
    while len(selected) < count and files:
        for f in files:
            selected.append(f)
            if len(selected) == count: break
    return selected

def get_all_audios(yt_id):
    path = get_channel_folder(yt_id, "audios")
    files = [os.path.join(path, f) for f in os.listdir(path) if f.lower().endswith(('.mp3', '.wav'))]
    random.shuffle(files)
    return files

def get_and_consume_thumbnail(yt_id):
    path = get_channel_folder(yt_id, "thumbnails")
    files = sorted([f for f in os.listdir(path) if f.lower().endswith(('.jpg', '.png', '.jpeg'))])
    if not files: return None
    return os.path.join(path, files[0])

def get_random_preset(allowed_names=None):
    if not os.path.exists(PRESETS_FILE): return None
    try:
        with open(PRESETS_FILE, 'r') as f: presets = json.load(f)
        if not presets: return None
        if allowed_names:
            filtered = {k: v for k, v in presets.items() if k in allowed_names}
            if filtered: return random.choice(list(filtered.values()))
        return random.choice(list(presets.values()))
    except: return None

def get_smart_preset(audio_path):
    """Menganalisis audio dan memilih preset yang cocok secara cerdas"""
    preset = None
    try:
        y, sr = librosa.load(audio_path, sr=22050, mono=True, duration=30)
        # deteksi BPM
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        # energi rata-rata
        rms = np.sqrt(np.mean(y**2))
        energy = min(1.0, rms * 5)
        # spectral centroid (brightness)
        cent = np.mean(librosa.feature.spectral_centroid(y=y, sr=sr))
        brightness = min(1.0, cent / 3000)

        if tempo < 80:
            # slow / chill
            preset = {
                "effect_type": random.choice(["waveform", "mirror", "smooth_blob", "sinusoidal"]),
                "particle_type": random.choice(["petals", "smoke", "snow", "sparkle"]),
                "bar_style": "center",
                "reactivity": round(random.uniform(0.4, 0.7), 2),
                "gravity": round(random.uniform(0.04, 0.10), 2),
            }
        elif tempo < 120:
            # medium
            preset = {
                "effect_type": random.choice(["spectrum", "circular", "dots_pixel", "filled_wave"]),
                "particle_type": random.choice(["sparkle", "trail", "bubbles", "petals"]),
                "bar_style": "bottom",
                "reactivity": round(random.uniform(0.6, 0.9), 2),
                "gravity": round(random.uniform(0.06, 0.14), 2),
            }
        else:
            # fast / energetic
            energy_boost = min(1.0, energy * 1.3)
            preset = {
                "effect_type": random.choice(["sunburst", "neon_glow", "halftone", "pixel"]),
                "particle_type": random.choice(["fireworks", "trail", "rain", "sparkle"]),
                "bar_style": "bottom",
                "reactivity": round(random.uniform(0.8, 1.5), 2),
                "gravity": round(random.uniform(0.10, 0.20), 2),
            }

        if preset:
            # warna berdasarkan brightness
            if brightness > 0.6:
                preset["color_bot"] = random.choice(["#ff6b6b", "#f093fb", "#4facfe", "#fa709a"])
                preset["color_top"] = random.choice(["#00f2fe", "#4facfe", "#f093fb", "#fa709a"])
            else:
                preset["color_bot"] = random.choice(["#10b981", "#00d4ff", "#7c5cfc", "#06d6a0"])
                preset["color_top"] = random.choice(["#00e5ff", "#7c5cfc", "#10b981", "#7209b7"])

            preset["color_part"] = "#ffffff"
            preset["pos_x"] = 50
            preset["pos_y"] = 85
            preset["width_pct"] = 60
            preset["max_height"] = 40
            preset["idle_height"] = 5
            preset["bar_count"] = random.choice([48, 64, 80])
            preset["spacing"] = random.choice([2, 3, 4])
            preset["part_amount"] = random.choice([3, 5, 8])
            preset["part_speed"] = round(random.uniform(0.5, 1.5), 1)
            preset["smoothing"] = 0.90
            preset["use_beat_pulse"] = random.choice([True, False])
            preset["fade_duration"] = 0
            preset["use_watermark"] = False
            preset["wm_text"] = ""
            preset["wm_color"] = "#ffffff"
            preset["wm_font"] = "M"
            preset["wm_size"] = 24
            preset["wm_position"] = "bl"
            preset["wm_move"] = "none"
            preset["use_tracklist"] = False
            preset["tl_font"] = "M"
            preset["tl_size"] = "medium"
            preset["tl_position"] = "tr"
            preset["tl_bg"] = "dark"
            preset["tl_title"] = "PLAYLIST"

        return preset
    except:
        return None

# ==========================================
# ⚙️ CORE ENGINE (VISUALIZER & FFMPEG)
# ==========================================
class AudioBrain:
    def __init__(self):
        self.y = None; self.sr = None; self.onset_env = None; self.has_audio = False
        self.duration = 0.0

    def load(self, path, max_duration=None):
        try:
            self.y, self.sr = librosa.load(path, sr=22050, mono=True, duration=max_duration)
            self.onset_env = librosa.onset.onset_strength(y=self.y, sr=self.sr)
            self.duration = len(self.y) / self.sr
            self.has_audio = True
        except Exception as e:
            print(f"Audio Error: {e}")

    def get_data(self, t, n_bars=64): 
        if not self.has_audio: return 0.0, False, np.zeros(n_bars)
        idx = int(t * self.sr)
        if idx >= len(self.y): return 0.0, False, np.zeros(n_bars)

        try: chunk = self.y[idx:idx+1024]; vol = np.sqrt(np.mean(chunk**2)) * 10 if len(chunk)>0 else 0
        except: vol = 0
        
        hit = False
        try:
            if int(idx/512) < len(self.onset_env) and self.onset_env[int(idx/512)] > 2.0: 
                hit = True
        except: pass

        final_bars = np.zeros(n_bars)
        try:
            n_fft = 2048; fft_data = self.y[idx:idx+n_fft]
            if len(fft_data) == n_fft:
                windowed_data = fft_data * np.hanning(n_fft)
                spec = np.abs(np.fft.rfft(windowed_data))
                usable = spec[2:200] 
                ls = len(usable)
                
                if ls > 0:
                    half_n = n_bars // 2
                    raw_bars = np.zeros(half_n)
                    for i in range(half_n):
                        s = int((i / half_n) * ls)
                        e = int(((i + 1) / half_n) * ls)
                        if e <= s: e = s + 1
                        if e > ls: e = ls
                        raw_bars[i] = np.mean(usable[s:e]) / 15.0 if e > s else 0
                    
                    smooth_half = np.convolve(raw_bars, np.ones(3)/3, mode='same')
                    final_bars = np.concatenate((smooth_half[::-1], smooth_half))
                    
                    if len(final_bars) < n_bars: final_bars = np.append(final_bars, 0)
                    elif len(final_bars) > n_bars: final_bars = final_bars[:n_bars]
        except: pass
                
        return vol, hit, final_bars

class BackgroundManager:
    def __init__(self, bg_paths, w, h):
        self.bg_paths = bg_paths; self.w = w; self.h = h; self.idx = 0; self.reader = None; self.static_bg = None; self.load_current()
        
    def load_current(self):
        if self.reader: self.reader.close()
        path = self.bg_paths[self.idx]
        if path.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')): 
            img = cv2.imread(path)
            if img is not None:
                self.static_bg = cv2.resize(img, (self.w, self.h))
            else:
                self.static_bg = np.zeros((self.h, self.w, 3), dtype=np.uint8)
        else: 
            self.reader = imageio.get_reader(path, 'ffmpeg')
            
    def get_frame(self):
        if self.static_bg is not None: return self.static_bg.copy()
        try: return cv2.resize(cv2.cvtColor(self.reader.get_next_data(), cv2.COLOR_RGB2BGR), (self.w, self.h))
        except: self.idx = (self.idx + 1) % len(self.bg_paths); self.load_current(); return self.get_frame()
        
    def close(self):
        if self.reader: self.reader.close()

class VisualEngine:
    def __init__(self, c_bot, c_top, c_part):
        self.col_bot = (c_bot[2], c_bot[1], c_bot[0])
        self.col_top = (c_top[2], c_top[1], c_top[0])
        self.col_part = (c_part[2], c_part[1], c_part[0])
        self.bar_h = None

        self.grad = np.zeros((1000, 1, 3), dtype=np.uint8)
        for c in range(3):
            self.grad[:, 0, c] = np.linspace(self.col_top[c], self.col_bot[c], 1000)

        self.particles = []

    # ── helper ──
    @staticmethod
    def _sn(val, default):
        try: return float(val) if val != "" and val is not None else default
        except: return default

    # ── helper warna per bar (interpolasi gradasi) ──
    def _bar_color(self, i, n):
        t = i / max(1, n - 1)
        r = int(self.col_top[0] * (1 - t) + self.col_bot[0] * t)
        g = int(self.col_top[1] * (1 - t) + self.col_bot[1] * t)
        b = int(self.col_top[2] * (1 - t) + self.col_bot[2] * t)
        return (r, g, b)

    # ── dispatcher utama ──
    def process(self, frame, vol, is_hit, bars, cfg):
        h, w = frame.shape[:2]
        n = len(bars)
        if self.bar_h is None or len(self.bar_h) != n:
            self.bar_h = np.zeros(n)

        react  = self._sn(cfg.get('reactivity'), 0.66)
        idle   = int(self._sn(cfg.get('idle_height'), 5))
        space  = int(self._sn(cfg.get('spacing'), 3))
        px     = self._sn(cfg.get('pos_x'), 50) / 100
        py     = self._sn(cfg.get('pos_y'), 85) / 100
        wp     = self._sn(cfg.get('width_pct'), 60) / 100
        max_h  = h * (self._sn(cfg.get('max_height'), 40) / 100)
        p_amt  = int(self._sn(cfg.get('part_amount'), 3))
        p_spd  = self._sn(cfg.get('part_speed'), 1.0)
        smooth = self._sn(cfg.get('smoothing'), 0.90)

        # smooth bar heights
        for i in range(n):
            target = bars[i] * react
            self.bar_h[i] = (self.bar_h[i] * smooth) + (target * (1 - smooth))
            self.bar_h[i] = max(0, self.bar_h[i])

        # beat pulse (additive, bisa aktif bersama efek lain)
        if cfg.get('use_beat_pulse', False):
            self._draw_beat_pulse(frame, vol, is_hit, w, h)

        # dispatch ke efek utama
        effect = cfg.get('effect_type', 'spectrum')
        if effect == 'circular':
            self._draw_circular(frame, n, idle, space, px, py, wp, max_h, w, h)
        elif effect == 'waveform':
            self._draw_waveform(frame, n, idle, space, px, py, wp, max_h, w, h)
        elif effect == 'mirror':
            self._draw_mirror(frame, n, idle, space, px, py, wp, max_h, w, h)
        elif effect == 'neon_glow':
            self._draw_neon_glow(frame, n, idle, space, px, py, wp, max_h, w, h)
        elif effect == 'sunburst':
            self._draw_sunburst(frame, n, idle, space, px, py, wp, max_h, w, h)
        elif effect == 'pixel':
            self._draw_pixel(frame, n, idle, space, px, py, wp, max_h, w, h)
        elif effect == 'double_symmetric':
            self._draw_double_symmetric(frame, n, idle, space, px, py, wp, max_h, w, h)
        elif effect == 'dots_pixel':
            self._draw_dots_pixel(frame, n, idle, space, px, py, wp, max_h, w, h)
        elif effect == 'filled_wave':
            self._draw_filled_wave(frame, n, idle, space, px, py, wp, max_h, w, h)
        elif effect == 'sinusoidal':
            self._draw_sinusoidal(frame, n, idle, space, px, py, wp, max_h, w, h)
        elif effect == 'smooth_blob':
            self._draw_smooth_blob(frame, n, idle, space, px, py, wp, max_h, w, h)
        elif effect == 'halftone':
            self._draw_halftone(frame, n, idle, space, px, py, wp, max_h, w, h)
        else:
            bar_style = cfg.get('bar_style', 'bottom')
            self._draw_spectrum(frame, n, idle, space, px, py, wp, max_h, w, h, bar_style)

        # sparkle / particles
        if p_amt > 0:
            p_type = cfg.get('particle_type', 'sparkle')
            self._draw_particles(frame, vol, is_hit, p_amt, p_spd, w, h, p_type, cfg)

        return frame

    # ═══════════════════════════════════════════════════════════
    #  SPECTRUM BARS  (efek asli)
    # ═══════════════════════════════════════════════════════════
    def _draw_spectrum(self, frame, n, idle, space, px, py, wp, max_h, w, h, bar_style):
        bar_w = int(max(1, (w * wp - space * (n - 1)) / n))
        s_x   = int((w * px) - (w * wp / 2))
        b_y   = int(h * py)

        for i in range(n):
            height = int(max(idle, min(max_h, self.bar_h[i] * max_h)))
            if height <= 0: continue
            x1 = s_x + i * (bar_w + space)
            x2 = x1 + bar_w

            if bar_style == 'center':
                y1 = b_y - (height // 2); y2 = b_y + (height // 2)
            else:
                y1 = b_y - height; y2 = b_y

            x1s = max(0, min(w, x1)); x2s = max(0, min(w, x2))
            y1s = max(0, min(h, y1)); y2s = max(0, min(h, y2))
            ws = x2s - x1s; hs = y2s - y1s
            if ws > 0 and hs > 0:
                bg = cv2.resize(self.grad, (bar_w, height))
                frame[y1s:y2s, x1s:x2s] = bg[y1s-y1:y1s-y1+hs, x1s-x1:x1s-x1+ws]

    # ═══════════════════════════════════════════════════════════
    #  CIRCULAR SPECTRUM  (bar radial membentuk lingkaran)
    # ═══════════════════════════════════════════════════════════
    def _draw_circular(self, frame, n, idle, space, px, py, wp, max_h, w, h):
        cx = int(w * px)
        cy = int(h * py)
        radius = int((w * wp) / 2)
        angle_step = (2 * math.pi) / n
        bar_w = max(2, int((2 * math.pi * radius - space * n) / n))

        for i in range(n):
            height = int(max(idle, min(max_h * 0.5, self.bar_h[i] * max_h)))
            if height <= 0: continue
            angle = i * angle_step - math.pi / 2

            # inner & outer edge
            ix = int(cx + radius * math.cos(angle))
            iy = int(cy + radius * math.sin(angle))
            ox = int(cx + (radius + height) * math.cos(angle))
            oy = int(cy + (radius + height) * math.sin(angle))

            color = self._bar_color(i, n)
            cv2.line(frame, (ix, iy), (ox, oy), color, bar_w)

        # lingkaran dasar tipis
        cv2.circle(frame, (cx, cy), radius, self.col_bot, 1)

    # ═══════════════════════════════════════════════════════════
    #  WAVEFORM  (gelombang audio klasik)
    # ═══════════════════════════════════════════════════════════
    def _draw_waveform(self, frame, n, idle, space, px, py, wp, max_h, w, h):
        tot_w = w * wp
        s_x   = int((w * px) - (tot_w / 2))
        b_y   = int(h * py)
        step  = tot_w / n

        pts_upper = []
        pts_lower = []
        for i in range(n):
            height = self.bar_h[i] * max_h
            height = max(idle, min(max_h, height))
            x = int(s_x + i * step)
            pts_upper.append((x, int(b_y - height)))
            pts_lower.append((x, int(b_y + height)))

        # fill area antara waveform
        pts_fill = pts_upper + pts_lower[::-1]
        if len(pts_fill) >= 3:
            overlay = frame.copy()
            cv2.fillPoly(overlay, [np.array(pts_fill, dtype=np.int32)], (*self.col_bot,))
            cv2.addWeighted(overlay, 0.25, frame, 0.75, 0, frame)

        # garis utama (atas) tebal
        pts_line_upper = np.array(pts_upper, dtype=np.int32)
        if len(pts_line_upper) >= 2:
            cv2.polylines(frame, [pts_line_upper], False, self.col_top, 2)

        # garis bawah tipis (refleksi)
        pts_line_lower = np.array(pts_lower, dtype=np.int32)
        if len(pts_line_lower) >= 2:
            cv2.polylines(frame, [pts_line_lower], False, self.col_bot, 1)

    # ═══════════════════════════════════════════════════════════
    #  MIRROR SPECTRUM  (bar spectrum + refleksi simetris)
    # ═══════════════════════════════════════════════════════════
    def _draw_mirror(self, frame, n, idle, space, px, py, wp, max_h, w, h):
        bar_w = int(max(1, (w * wp - space * (n - 1)) / n))
        s_x   = int((w * px) - (w * wp / 2))
        b_y   = int(h * py)
        half_h = max_h // 2

        for i in range(n):
            height = int(max(idle, min(half_h, self.bar_h[i] * half_h)))
            if height <= 0: continue
            x1 = s_x + i * (bar_w + space)
            x2 = x1 + bar_w

            # bar ke atas (full opacity)
            y1_up = b_y - height; y2_up = b_y
            x1s = max(0, min(w, x1)); x2s = max(0, min(w, x2))
            y1s = max(0, min(h, y1_up)); y2s = max(0, min(h, y2_up))
            ws = x2s - x1s; hs = y2s - y1s
            if ws > 0 and hs > 0:
                bg = cv2.resize(self.grad, (bar_w, height))
                frame[y1s:y2s, x1s:x2s] = bg[y1s-y1_up:y1s-y1_up+hs, x1s-x1:x1s-x1+ws]

            # bar refleksi ke bawah (lebih redup)
            y1_dn = b_y; y2_dn = b_y + height
            x1s2 = max(0, min(w, x1)); x2s2 = max(0, min(w, x2))
            y1s2 = max(0, min(h, y1_dn)); y2s2 = max(0, min(h, y2_dn))
            ws2 = x2s2 - x1s2; hs2 = y2s2 - y1s2
            if ws2 > 0 and hs2 > 0:
                bg2 = cv2.resize(self.grad, (bar_w, height))
                faded = (bg2 * 0.35).astype(np.uint8)
                frame[y1s2:y2s2, x1s2:x2s2] = faded[y1s2-y1_dn:y1s2-y1_dn+hs2, x1s2-x1:x1s2-x1+ws2]

    # ═══════════════════════════════════════════════════════════
    #  NEON GLOW BARS  (bar dengan efek glow/bloom seperti neon)
    # ═══════════════════════════════════════════════════════════
    def _draw_neon_glow(self, frame, n, idle, space, px, py, wp, max_h, w, h):
        # gambar di overlay terpisah lalu blur untuk efek bloom
        overlay = np.zeros_like(frame)
        bar_w = int(max(1, (w * wp - space * (n - 1)) / n))
        s_x   = int((w * px) - (w * wp / 2))
        b_y   = int(h * py)

        for i in range(n):
            height = int(max(idle, min(max_h, self.bar_h[i] * max_h)))
            if height <= 0: continue
            x1 = s_x + i * (bar_w + space)
            x2 = x1 + bar_w
            y1 = b_y - height; y2 = b_y

            x1s = max(0, min(w, x1)); x2s = max(0, min(w, x2))
            y1s = max(0, min(h, y1)); y2s = max(0, min(h, y2))
            ws = x2s - x1s; hs = y2s - y1s
            if ws > 0 and hs > 0:
                bg = cv2.resize(self.grad, (bar_w, height))
                overlay[y1s:y2s, x1s:x2s] = bg[y1s-y1:y1s-y1+hs, x1s-x1:x1s-x1+ws]

        # bloom layer (blur)
        bloom = cv2.GaussianBlur(overlay, (21, 21), 0)
        cv2.addWeighted(bloom, 0.7, frame, 1.0, 0, frame)
        # core layer (tajam)
        cv2.addWeighted(overlay, 0.9, frame, 1.0, 0, frame)

    # ═══════════════════════════════════════════════════════════
    #  RADIAL SUNBURST  (memancar dari pusat ke segala arah)
    # ═══════════════════════════════════════════════════════════
    def _draw_sunburst(self, frame, n, idle, space, px, py, wp, max_h, w, h):
        cx = int(w * px)
        cy = int(h * py)
        max_r = int((w * wp) / 2)
        angle_step = (2 * math.pi) / n

        for i in range(n):
            height = int(max(idle, min(max_h * 0.6, self.bar_h[i] * max_h)))
            if height <= 0: continue
            angle = i * angle_step - math.pi / 2

            # dari pusat (radius kecil) memancar keluar
            inner_r = max_r * 0.15
            ix = int(cx + inner_r * math.cos(angle))
            iy = int(cy + inner_r * math.sin(angle))
            ox = int(cx + (inner_r + height) * math.cos(angle))
            oy = int(cy + (inner_r + height) * math.sin(angle))

            color = self._bar_color(i, n)
            cv2.line(frame, (ix, iy), (ox, oy), color, 3)

    # ═══════════════════════════════════════════════════════════
    #  PIXEL BLOCKS  (8-bit retro, blok kotak besar)
    # ═══════════════════════════════════════════════════════════
    def _draw_pixel(self, frame, n, idle, space, px, py, wp, max_h, w, h):
        # kurangi bar count untuk efek blocky, pakai blok besar
        block_size = max(6, int(w * wp / n))
        gap = max(2, space)
        bar_w = block_size - gap
        s_x   = int((w * px) - (w * wp / 2))
        b_y   = int(h * py)
        block_unit = max(4, int(max_h / 16))  # tinggi per pixel block

        for i in range(n):
            height = int(max(idle, min(max_h, self.bar_h[i] * max_h)))
            if height <= 0: continue
            x1 = s_x + i * (block_size)

            blocks = max(1, height // block_unit)
            for b in range(blocks):
                y_block = b_y - (b + 1) * block_unit
                if y_block < 0: break
                # warna gradient per block
                t = b / max(1, blocks)
                color = (
                    int(self.col_top[0] * (1-t) + self.col_bot[0] * t),
                    int(self.col_top[1] * (1-t) + self.col_bot[1] * t),
                    int(self.col_top[2] * (1-t) + self.col_bot[2] * t),
                )
                cv2.rectangle(frame, (x1, y_block), (x1 + bar_w, y_block + block_unit - gap), color, -1)

    # ═══════════════════════════════════════════════════════════
    #  DOUBLE SYMMETRIC  (mirror kiri-kanan dari garis tengah vertikal)
    # ═══════════════════════════════════════════════════════════
    def _draw_double_symmetric(self, frame, n, idle, space, px, py, wp, max_h, w, h):
        half = n // 2
        bar_w = int(max(1, (w * wp - space * (n - 1)) / n))
        s_x   = int((w * px) - (w * wp / 2))
        b_y   = int(h * py)

        for i in range(n):
            height = int(max(idle, min(max_h, self.bar_h[i] * max_h)))
            if height <= 0: continue
            x1 = s_x + i * (bar_w + space)
            y1 = b_y - height; y2 = b_y

            x1s = max(0, min(w, x1)); x2s = max(0, min(w, x1 + bar_w))
            y1s = max(0, min(h, y1)); y2s = max(0, min(h, y2))
            ws = x2s - x1s; hs = y2s - y1s
            if ws > 0 and hs > 0:
                bg = cv2.resize(self.grad, (bar_w, height))
                frame[y1s:y2s, x1s:x2s] = bg[y1s-y1:y1s-y1+hs, x1s-x1:x1s-x1+ws]

            # mirror ke kanan dari garis tengah vertikal
            mid_x = int(w * px)
            m_x1 = 2 * mid_x - x1 - bar_w
            m_x2 = m_x1 + bar_w
            m_x1s = max(0, min(w, m_x1)); m_x2s = max(0, min(w, m_x2))
            mws = m_x2s - m_x1s
            if mws > 0 and hs > 0:
                frame[y1s:y2s, m_x1s:m_x2s] = bg[y1s-y1:y1s-y1+hs, m_x1s-m_x1:m_x1s-m_x1+mws]

    # ═══════════════════════════════════════════════════════════
    #  DOTS PIXEL  (lingkaran di puncak bar)
    # ═══════════════════════════════════════════════════════════
    def _draw_dots_pixel(self, frame, n, idle, space, px, py, wp, max_h, w, h):
        bar_w = max(2, int((w * wp - space * (n - 1)) / n))
        s_x   = int((w * px) - (w * wp / 2))
        b_y   = int(h * py)
        for i in range(n):
            height = int(max(idle, min(max_h, self.bar_h[i] * max_h)))
            if height <= 0: continue
            cx = s_x + i * (bar_w + space) + bar_w // 2
            cy = b_y - height
            r = max(2, int(height * 0.3))
            color = self._bar_color(i, n)
            cv2.circle(frame, (cx, cy), r, color, -1)

    # ═══════════════════════════════════════════════════════════
    #  FILLED WAVE  (gelombang terisi penuh)
    # ═══════════════════════════════════════════════════════════
    def _draw_filled_wave(self, frame, n, idle, space, px, py, wp, max_h, w, h):
        tot_w = w * wp
        s_x   = int((w * px) - (tot_w / 2))
        b_y   = int(h * py)
        step  = tot_w / n

        pts = []
        for i in range(n):
            height = max(idle, min(max_h, self.bar_h[i] * max_h))
            x = int(s_x + i * step)
            pts.append((x, int(b_y - height)))
        # tutup polygon dari kanan bawah ke kiri bawah
        pts.append((int(s_x + tot_w), b_y))
        pts.append((s_x, b_y))

        if len(pts) >= 3:
            overlay_wave = frame.copy()
            cv2.fillPoly(overlay_wave, [np.array(pts, dtype=np.int32)], self.col_bot)
            cv2.addWeighted(overlay_wave, 0.55, frame, 0.45, 0, frame)

        # garis atas
        for i in range(1, n):
            h1 = max(idle, min(max_h, self.bar_h[i-1] * max_h))
            h2 = max(idle, min(max_h, self.bar_h[i] * max_h))
            x1 = int(s_x + (i-1) * step)
            x2 = int(s_x + i * step)
            cv2.line(frame, (x1, int(b_y - h1)), (x2, int(b_y - h2)), self.col_top, 3)

    # ═══════════════════════════════════════════════════════════
    #  SINUSOIDAL  (gelombang sinusoidal berfrekuensi audio)
    # ═══════════════════════════════════════════════════════════
    def _draw_sinusoidal(self, frame, n, idle, space, px, py, wp, max_h, w, h):
        tot_w = w * wp
        s_x   = int((w * px) - (tot_w / 2))
        b_y   = int(h * py)
        amp   = max_h * 0.4
        freq  = 4.0  # jumlah gelombang

        pts = []
        for i in range(n):
            t = i / max(1, n - 1)
            modulation = self.bar_h[i]  # 0..1 dari audio
            wave = math.sin(t * freq * 2 * math.pi + math.pi * 0.5) * modulation * amp
            height = max(idle, wave + amp * modulation * 0.3)
            x = int(s_x + i * (tot_w / n))
            y = int(b_y - height)
            pts.append((x, y))

        if len(pts) >= 2:
            cv2.polylines(frame, [np.array(pts, dtype=np.int32)], False, self.col_top, 2)
            # glow tipis
            overlay_sin = frame.copy()
            for i in range(n):
                h = max(0, b_y - pts[i][1])
                cv2.line(overlay_sin, (pts[i][0], b_y), (pts[i][0], pts[i][1]), self.col_bot, max(1, int(h * 0.15)))
            cv2.addWeighted(overlay_sin, 0.3, frame, 0.7, 0, frame)

    # ═══════════════════════════════════════════════════════════
    #  SMOOTH BLOB  (gumpalan organik halus)
    # ═══════════════════════════════════════════════════════════
    def _draw_smooth_blob(self, frame, n, idle, space, px, py, wp, max_h, w, h):
        cx = int(w * px)
        cy = int(h * py)
        base_r = min(w, h) * 0.08
        blob_amp = min(w, h) * 0.25

        angle_step = (2 * math.pi) / n
        pts = []
        for i in range(n):
            angle = i * angle_step - math.pi / 2
            r_offset = self.bar_h[i] * blob_amp
            r = base_r + r_offset
            x = int(cx + r * math.cos(angle))
            y = int(cy + r * math.sin(angle))
            pts.append([x, y])

        if len(pts) >= 3:
            overlay_blob = frame.copy()
            cv2.fillPoly(overlay_blob, [np.array(pts, dtype=np.int32)], self.col_bot)
            # blur untuk efek halus
            overlay_blob = cv2.GaussianBlur(overlay_blob, (15, 15), 0)
            cv2.addWeighted(overlay_blob, 0.6, frame, 1.0, 0, frame)
            # core outline
            cv2.polylines(frame, [np.array(pts, dtype=np.int32)], True, self.col_top, 2)

    # ═══════════════════════════════════════════════════════════
    #  HALFTONE DOTS  (titik-titik yang membesar oleh audio)
    # ═══════════════════════════════════════════════════════════
    def _draw_halftone(self, frame, n, idle, space, px, py, wp, max_h, w, h):
        cols = int(n * 0.5)
        rows = max(3, int(cols * 0.4))
        cell_w = int(w * wp / cols)
        cell_h = int(max_h * 0.8 / rows)
        start_x = int(w * px) - (cols * cell_w) // 2
        start_y = int(h * py) - (rows * cell_h) // 2
        max_dot = min(cell_w, cell_h) * 0.45

        for r in range(rows):
            for c in range(cols):
                idx = (r * cols + c) % n
                amp = min(1.0, self.bar_h[idx] / max(max_h, 1))
                dot_r = max(1, int(1 + amp * max_dot))
                dx = start_x + c * cell_w + cell_w // 2
                dy = start_y + r * cell_h + cell_h // 2
                t = idx / max(1, n - 1)
                color = self._bar_color(int(idx), int(n))
                cv2.circle(frame, (dx, dy), dot_r, color, -1)

    # ═══════════════════════════════════════════════════════════
    #  BEAT PULSE / GLOW  (overlay flash saat bass hit)
    # ═══════════════════════════════════════════════════════════
    def _draw_beat_pulse(self, frame, vol, is_hit, w, h):
        if is_hit and vol > 1.2:
            intensity = min(0.18, vol * 0.04)
            overlay = frame.copy()
            overlay[:] = self.col_top
            cv2.addWeighted(overlay, intensity, frame, 1.0 - intensity, 0, frame)

    # ═══════════════════════════════════════════════════════════
    #  SPARKLE / PARTICLES  (multi-tipe)
    # ═══════════════════════════════════════════════════════════
    def _draw_particles(self, frame, vol, is_hit, p_amt, p_spd, w, h, p_type='sparkle', cfg=None):
        # particle settings
        sz_mult = {'small': 0.6, 'large': 1.5}.get(cfg.get('part_size', 'medium') if cfg else 'medium', 1.0)
        life_mult = {'short': 0.5, 'long': 1.8}.get(cfg.get('part_life', 'medium') if cfg else 'medium', 1.0)
        dens_mult = {'low': 0.5, 'high': 1.8}.get(cfg.get('part_density', 'medium') if cfg else 'medium', 1.0)
        part_alpha = float(cfg.get('part_opacity', 1.0)) if cfg else 1.0

        # spawn particles
        if is_hit and vol > 1.5:
            for _ in range(max(1, min(int(p_amt * dens_mult), 15))):
                if p_type == 'fireworks':
                    self.particles.append([
                        np.random.randint(w*0.3, w*0.7), np.random.randint(h*0.6, h*0.9),
                        np.random.uniform(-6, 6), np.random.uniform(-8, -2),
	                        int((np.random.randint(2, 5)) * sz_mult), int((30 + random.random() * 30) * life_mult), 0, 'fw'
                    ])
                elif p_type == 'trail':
                    self.particles.append([
                        np.random.randint(0, w), np.random.randint(0, h//2),
                        np.random.uniform(-4, 4), np.random.uniform(2, 6),
                        np.random.randint(2, 4), np.random.randint(40, 70), 0, 'tr'
                    ])
                elif p_type == 'petals':
                    self.particles.append([
                        np.random.randint(0, w), -10,
                        np.random.uniform(-1, 1), np.random.uniform(0.5, 2),
                        np.random.randint(3, 6), np.random.randint(80, 120), np.random.uniform(0, math.pi), 'pt'
                    ])
                elif p_type == 'smoke':
                    # asap: dari dasar, naik perlahan, membesar, memudar
                    self.particles.append([
                        np.random.randint(w*0.1, w*0.9), np.random.randint(h*0.7, h),
                        np.random.uniform(-0.5, 0.5), np.random.uniform(-1.5, -0.3),
                        2, np.random.randint(40, 80), np.random.uniform(0.02, 0.08), 'sm'
                    ])
                elif p_type == 'snow':
                    # salju: dari atas, turun perlahan, goyang
                    self.particles.append([
                        np.random.randint(0, w), -10,
                        np.random.uniform(-0.8, 0.8), np.random.uniform(0.3, 1.2),
                        np.random.randint(1, 3), int((h + 20) / (0.3 + random.uniform(0.3, 1.2))) + 30, 0, 'sn'
                    ])
                elif p_type == 'rain':
                    # hujan: garis tipis dari atas, cepat, miring sedikit
                    self.particles.append([
                        np.random.randint(0, w), -20,
                        np.random.uniform(-1.5, -0.3), np.random.uniform(5, 9),
                        np.random.randint(1, 2), int((h + 40) / (5 + random.uniform(0, 4))) + 15, 0, 'rn'
                    ])
                elif p_type == 'bubbles':
                    # gelembung: dari dasar, naik, goyang, border transparan
                    self.particles.append([
                        np.random.randint(w*0.1, w*0.9), np.random.randint(h*0.8, h),
                        np.random.uniform(-0.6, 0.6), np.random.uniform(-1.8, -0.5),
                        np.random.randint(3, 8), np.random.randint(60, 120), 0, 'bb'
                    ])
                else:  # sparkle (default)
                    self.particles.append([
                        np.random.randint(0, w), np.random.randint(0, h),
                        np.random.uniform(-3, 3), np.random.uniform(-3, 3),
                        np.random.randint(2, 6), 0, 0, 'sp'
                    ])

        alive = []
        spd = 1.0 + (vol * 0.1 * p_spd)
        for p in self.particles:
            x, y, vx, vy = p[0], p[1], p[2], p[3]
            radius = p[4]
            life = p[5] if len(p) > 5 else 0
            extra = p[6] if len(p) > 6 else 0
            ptype = p[7] if len(p) > 7 else 'sp'

            x += vx * spd; y += vy * spd

            if ptype == 'fw':
                vy += 0.25 * spd
                radius -= 0.05
                life -= 1
                if radius > 0 and life > 0 and y < h + 20:
                    cv2.circle(frame, (int(x), int(y)), max(1, int(radius)), self.col_part, -1)
                    alive.append([x, y, vx, vy, radius, life, extra, ptype])
            elif ptype == 'tr':
                radius -= 0.03
                life -= 1
                if radius > 0 and life > 0:
                    cv2.circle(frame, (int(x), int(y)), max(1, int(radius)), self.col_top, -1)
                    cv2.line(frame, (int(x), int(y)), (int(x - vx*2), int(y - vy*2)), self.col_part, 1)
                    alive.append([x, y, vx, vy, radius, life, extra, ptype])
            elif ptype == 'pt':
                x += math.sin(y * 0.05) * 0.8
                radius -= 0.005
                life -= 1
                extra += 0.08
                if radius > 0 and life > 0 and y < h + 20:
                    self._draw_star(frame, int(x), int(y), max(1, int(radius)), extra, self.col_part)
                    alive.append([x, y, vx, vy, radius, life, extra, ptype])
            elif ptype == 'sm':
                # smoke: membesar & memudar — tampilan asap lembut
                radius += extra * spd
                life -= 1
                if radius < 100 and life > 0 and y > -20:
                    fade = max(0.1, life / 60)
                    # buat overlay kecil untuk smoke (agar bisa di-blur)
                    r_int = max(2, int(radius))
                    d = r_int * 2 + 6
                    x1 = max(0, min(w - d, int(x) - r_int - 3))
                    y1 = max(0, min(h - d, int(y) - r_int - 3))
                    smoke_patch = np.zeros((d, d, 3), dtype=np.uint8)
                    cx_sm, cy_sm = r_int + 3, r_int + 3
                    intensity = int(180 * fade)
                    cv2.circle(smoke_patch, (cx_sm, cy_sm), r_int, (intensity, intensity, intensity), -1)
                    # blur untuk efek soft
                    smoke_patch = cv2.GaussianBlur(smoke_patch, (0, 0), max(2, r_int // 3))
                    # blend ke frame
                    roi_sm = frame[y1:y1+d, x1:x1+d]
                    cv2.addWeighted(smoke_patch, 0.5 * fade, roi_sm, 1.0 - 0.5 * fade, 0, roi_sm)
                    alive.append([x, y, vx, vy, radius, life, extra, ptype])
            elif ptype == 'sn':
                # snow: goyang ringan
                x += math.sin(y * 0.08) * 0.5
                life -= 1
                if life > 0 and y < h + 10:
                    cv2.circle(frame, (int(x), int(y)), max(1, int(radius)), self.col_part, -1)
                    alive.append([x, y, vx, vy, radius, life, extra, ptype])
            elif ptype == 'rn':
                # rain: garis vertikal tipis
                life -= 1
                if life > 0 and y < h + 10:
                    cv2.line(frame, (int(x), int(y)), (int(x - vx), int(y - vy*0.3)), self.col_part, 1)
                    alive.append([x, y, vx, vy, radius, life, extra, ptype])
            elif ptype == 'bb':
                # bubbles: naik + goyang, border putih dengan fill transparan
                x += math.sin(y * 0.06) * 0.6
                radius -= 0.01
                life -= 1
                if radius > 1 and life > 0 and y > -10:
                    # fill transparent (mix with bg - we use thin outline instead)
                    cv2.circle(frame, (int(x), int(y)), max(1, int(radius)), self.col_part, 1)
                    cv2.circle(frame, (int(x), int(y)), max(1, int(radius)-1), self.col_part, -1)
                    # highlight spot di pojok
                    hs = max(1, int(radius * 0.3))
                    cv2.circle(frame, (int(x) - int(radius*0.3), int(y) - int(radius*0.3)), hs, (255, 255, 255), -1)
                    alive.append([x, y, vx, vy, radius, life, extra, ptype])
            else:
                # sparkle default
                radius -= 0.1
                if radius > 0:
                    cv2.circle(frame, (int(x), int(y)), int(radius), self.col_part, -1)
                    alive.append([x, y, vx, vy, radius, life, extra, ptype])
        self.particles = alive

    # ── helper: gambar bintang 5-kelopak untuk petals ──
    def _draw_star(self, frame, cx, cy, r, rotation, color):
        pts = []
        for i in range(10):
            angle = rotation + i * (math.pi / 5) - math.pi / 2
            rad = r if i % 2 == 0 else r * 0.45
            pts.append([int(cx + rad * math.cos(angle)), int(cy + rad * math.sin(angle))])
        cv2.fillPoly(frame, [np.array(pts, dtype=np.int32)], color)

def hex_to_rgb(h): return tuple(int(str(h).lstrip('#')[i:i+2], 16) for i in (0, 2, 4))

def render_video_core(task_id, audio_path, bg_paths, output_path, duration, cfg):
    w, h = 1280, 720; fps = 30; total_f = int(duration * fps)
    c_bot = hex_to_rgb(cfg.get('color_bot', '#10b981'))
    c_top = hex_to_rgb(cfg.get('color_top', '#0ea5e9'))
    c_part = hex_to_rgb(cfg.get('color_part', '#ffffff'))
    bar_c = int(cfg.get('bar_count', 64))
    vis = VisualEngine(c_bot, c_top, c_part)
    bg = BackgroundManager(bg_paths, w, h)
    audio = AudioBrain(); audio.load(audio_path)
    
    with db_lock:
        for d in active_tasks:
            if d['id'] == task_id: d['status'] = "Rendering Visual & Background... ⚡"
    save_tasks_db()

    cmd = [
        get_ffmpeg_path(), '-y', '-threads', '2', 
        '-f', 'rawvideo', '-vcodec', 'rawvideo', '-s', f'{w}x{h}', '-pix_fmt', 'bgr24', '-r', str(fps), 
        '-i', '-', 
        '-i', audio_path, 
        '-t', str(duration),
        '-c:v', 'libx264', '-preset', 'fast', '-pix_fmt', 'yuv420p', output_path
    ]
    
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    try:
        for f in range(total_f):
            if stop_flags.get(task_id):
                raise Exception("Dibatalkan")
                
            v, is_hit, bars = audio.get_data(f/fps, bar_c)
            frame = vis.process(bg.get_frame(), v, is_hit, bars, cfg)

            # ── TRANSISI FADE IN / FADE OUT ──
            try:
                fade_dur = float(cfg.get('fade_duration', 0) or 0)
            except (ValueError, TypeError):
                fade_dur = 0.0
            if fade_dur > 0:
                fade_frames = int(fade_dur * fps)
                # fade in (awal video: dari hitam ke normal)
                if f < fade_frames:
                    alpha = f / fade_frames
                    frame = (frame * alpha).astype(np.uint8)
                # fade out (akhir video: dari normal ke hitam)
                if f >= total_f - fade_frames:
                    alpha = (total_f - f) / fade_frames
                    frame = (frame * alpha).astype(np.uint8)

            if cfg.get('use_floating_card', False) and 'track_schedule' in cfg:
                sec = f / fps
                current_track = None
                for track in cfg['track_schedule']:
                    if track['start'] <= sec < track['end']:
                        current_track = track
                        break
                
                if current_track:
                    t = sec - current_track['start'] 
                    if t < 10.0:
                        alpha = (t * 0.85) if t < 1.0 else ((10.0 - t) * 0.85 if t > 9.0 else 0.85)
                        if alpha > 0.05:
                            cw, ch = 500, 100
                            x, y = 40, h - ch - 40
                            roi = frame[y:y+ch, x:x+cw]
                            overlay = roi.copy()
                            cv2.rectangle(overlay, (0, 0), (cw, ch), (30, 20, 15), -1)
                            cv2.rectangle(overlay, (15, 15), (85, 85), (60, 200, 80), -1)
                            card_title = current_track['title']
                            ch_name = cfg.get('channel_name', 'KeiBot FM')
                            cv2.putText(overlay, card_title[:35], (105, 45), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
                            cv2.putText(overlay, f"Now Playing . {ch_name}", (105, 75), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (180, 180, 180), 1, cv2.LINE_AA)
                            cv2.putText(overlay, "J", (36, 65), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (255, 255, 255), 3, cv2.LINE_AA)
                            cv2.addWeighted(overlay, alpha, roi, 1 - alpha, 0, roi)

            # ── TRACK LIST (daftar lagu di dalam video) ──
            if cfg.get('use_tracklist', False) and 'track_schedule' in cfg:
                sec = f / fps
                cur_idx = -1
                for idx, track in enumerate(cfg['track_schedule']):
                    if track['start'] <= sec < track['end']:
                        cur_idx = idx
                        break
                if cur_idx < 0:
                    # cari track terakhir jika sudah lewat semua
                    if cfg['track_schedule'] and sec >= cfg['track_schedule'][-1]['end']:
                        cur_idx = len(cfg['track_schedule']) - 1

                tracks = cfg['track_schedule']
                tl_pos = str(cfg.get('tl_position', 'tr'))
                tl_size = str(cfg.get('tl_size', 'medium'))
                tl_bg = str(cfg.get('tl_bg', 'dark'))
                tl_font = str(cfg.get('tl_font', 'M'))

                # size config
                if tl_size == 'large':
                    item_h = 32; list_w = 400; font_s = 0.5; header_s = 0.5
                elif tl_size == 'small':
                    item_h = 22; list_w = 280; font_s = 0.35; header_s = 0.4
                else:  # medium
                    item_h = 28; list_w = 360; font_s = 0.4; header_s = 0.45

                max_show = min(len(tracks), 10)
                pad = 10
                list_h = max_show * item_h + pad * 2 + 20

                # position
                margin = 20
                if tl_pos == 'tl': list_x, list_y = margin, margin
                elif tl_pos == 'bl': list_x, list_y = margin, h - list_h - margin
                elif tl_pos == 'br': list_x, list_y = w - list_w - margin, h - list_h - margin
                elif tl_pos == 'cl': list_x, list_y = margin, (h - list_h) // 2
                elif tl_pos == 'cr': list_x, list_y = w - list_w - margin, (h - list_h) // 2
                else: list_x, list_y = w - list_w - margin, margin  # tr default

                # font mapping
                tl_font_map = {
                    'M': cv2.FONT_HERSHEY_DUPLEX, 'S': cv2.FONT_HERSHEY_SIMPLEX,
                    'I': cv2.FONT_HERSHEY_TRIPLEX, 'C': cv2.FONT_HERSHEY_PLAIN,
                }
                tfont = tl_font_map.get(tl_font, cv2.FONT_HERSHEY_SIMPLEX)

                # background style
                overlay_list = np.zeros((list_h, list_w, 3), dtype=np.uint8)
                if tl_bg == 'glass':
                    cv2.rectangle(overlay_list, (0, 0), (list_w, list_h), (20, 15, 15), -1)
                    cv2.rectangle(overlay_list, (0, 0), (list_w, list_h), (80, 70, 70), 1)
                    blend_alpha = 0.55
                elif tl_bg == 'minimal':
                    cv2.rectangle(overlay_list, (0, 0), (list_w, list_h), (0, 0, 0), -1)
                    # hanya border tipis
                    cv2.rectangle(overlay_list, (0, 0), (list_w, list_h), (50, 50, 50), 1)
                    blend_alpha = 0.40
                else:  # dark
                    cv2.rectangle(overlay_list, (0, 0), (list_w, list_h), (15, 10, 10), -1)
                    cv2.rectangle(overlay_list, (0, 0), (list_w, list_h), (60, 50, 50), 1)
                    blend_alpha = 0.82

                # header
                cv2.putText(overlay_list, str(cfg.get('tl_title', 'Playlist')), (pad, pad + 12), tfont, header_s, (200, 200, 200), 1, cv2.LINE_AA)

                start_idx = max(0, cur_idx - 4)
                shown = 0
                for i in range(start_idx, min(len(tracks), start_idx + max_show)):
                    tr = tracks[i]
                    y0 = pad + 20 + shown * item_h
                    is_active = (i == cur_idx)

                    if is_active:
                        cv2.rectangle(overlay_list, (pad, y0), (list_w - pad, y0 + item_h - 2), (70, 140, 60), -1)
                        mark = "▶ "
                    else:
                        mark = "   "

                    num_str = f"{mark}{i+1}."
                    title_str = tr['title'][:22]
                    dur_str = f"{int(tr['duration']//60)}:{int(tr['duration']%60):02d}"

                    text_color = (255, 255, 255) if is_active else (180, 180, 180)
                    cv2.putText(overlay_list, num_str, (pad + 4, y0 + 14), tfont, font_s, text_color, 1, cv2.LINE_AA)
                    cv2.putText(overlay_list, title_str, (pad + 46, y0 + 14), tfont, font_s, text_color, 1, cv2.LINE_AA)
                    cv2.putText(overlay_list, dur_str, (list_w - pad - 40, y0 + 14), tfont, font_s, (150, 150, 150), 1, cv2.LINE_AA)
                    shown += 1

                # blend ke frame
                roi_list = frame[list_y:list_y + list_h, list_x:list_x + list_w]
                cv2.addWeighted(overlay_list, blend_alpha, roi_list, 1.0 - blend_alpha, 0, roi_list)

            # ── WATERMARK TEKS ──
            if cfg.get('use_watermark', False):
                wm_text = str(cfg.get('wm_text', ''))
                if wm_text:
                    wm_color_hex = cfg.get('wm_color', '#ffffff')
                    wm_color = hex_to_rgb(wm_color_hex)
                    wm_color_bgr = (wm_color[2], wm_color[1], wm_color[0])  # BGR untuk cv2
                    wm_size = int(cfg.get('wm_size', 24))
                    wm_pos = cfg.get('wm_position', 'bl')
                    wm_move = cfg.get('wm_move', 'none')
                    wm_font = cfg.get('wm_font', 'M')
                    wm_speed = cfg.get('wm_speed', 'medium')

                    # font mapping
                    font_map = {
                        'M': cv2.FONT_HERSHEY_DUPLEX,
                        'S': cv2.FONT_HERSHEY_SIMPLEX,
                        'I': cv2.FONT_HERSHEY_TRIPLEX,
                        'C': cv2.FONT_HERSHEY_PLAIN,
                    }
                    font = font_map.get(wm_font, cv2.FONT_HERSHEY_SIMPLEX)
                    thickness = max(1, wm_size // 14)

                    # ukuran teks untuk posisi
                    (tw, th), _ = cv2.getTextSize(wm_text, font, wm_size * 0.06, thickness)
                    margin = 30

                    # posisi dasar
                    base_positions = {
                        'tl': (margin, margin + th),
                        'tr': (w - tw - margin, margin + th),
                        'bl': (margin, h - margin),
                        'br': (w - tw - margin, h - margin),
                        'center': (w//2 - tw//2, h//2 + th//2),
                    }
                    bx, by = base_positions.get(wm_pos, (margin, h - margin))

                    # movement
                    frame_sec = f / fps
                    if wm_move == 'float':
                        offset = math.sin(frame_sec * 1.5) * 10
                        by += int(offset)
                    elif wm_move == 'scroll':
                        scroll_range = w + tw + margin * 2
                        offset = ((frame_sec * 40) % scroll_range) - tw - margin
                        bx = int(offset)
                    elif wm_move == 'pulse':
                        pulse = 0.7 + 0.3 * abs(math.sin(frame_sec * 2.5))
                    elif wm_move == 'random_walk':
                        # random walk kontinu
                        if not hasattr(self, '_wm_state'):
                            self._wm_state = {'x': float(bx), 'y': float(by), 'dx': 1.5, 'dy': 1.2}
                        ws = self._wm_state
                        wm_spd_mult = {'slow': 0.5, 'fast': 2.5}.get(wm_speed, 1.2)
                        # update arah gradual
                        if random.random() < 0.015:
                            a = random.random() * 2 * math.pi
                            ws['dx'] += (math.cos(a) * wm_spd_mult - ws['dx']) * 0.1
                            ws['dy'] += (math.sin(a) * wm_spd_mult - ws['dy']) * 0.1
                        ws['x'] += ws['dx']; ws['y'] += ws['dy']
                        # pantul di tepi
                        for side, limit, key in [(margin, w - tw - margin, 'x'), (margin + th, h - margin, 'y')]:
                            if ws[key] < side: ws[key] = side; (ws['dx'], ws['dy']) = (-ws['dx'], ws['dy']) if key == 'x' else (ws['dx'], -ws['dy'])
                            if ws[key] > limit: ws[key] = limit; (ws['dx'], ws['dy']) = (-ws['dx'], ws['dy']) if key == 'x' else (ws['dx'], -ws['dy'])
                        bx = int(ws['x']); by = int(ws['y'])
                        pulse = 1.0
                    else:
                        pulse = 1.0

                    # shadow
                    shadow_color = (0, 0, 0)
                    cv2.putText(frame, wm_text, (bx+2, by+2), font, wm_size * 0.06, shadow_color, thickness, cv2.LINE_AA)
                    # draw text
                    if wm_move == 'pulse':
                        cv2.putText(frame, wm_text, (bx, by), font, wm_size * 0.06 * pulse, wm_color_bgr, thickness, cv2.LINE_AA)
                    else:
                        cv2.putText(frame, wm_text, (bx, by), font, wm_size * 0.06, wm_color_bgr, thickness, cv2.LINE_AA)

            proc.stdin.write(frame.tobytes())
            
    except Exception as e:
        proc.stdin.close()
        proc.terminate()
        bg.close()
        raise e
        
    proc.stdin.close(); proc.wait(); bg.close()

# ==========================================
# 🚀 BACKGROUND WORKER: OTO-LOOP ULTIMATE
# ==========================================
def background_worker():
    global channel_cooldowns
    
    while True:
        task = render_queue.get()
        task_id = task['id']
        yt_id = task['yt_id']
        
        # 🔥 SMART COOLDOWN SYSTEM (PUTAR BALIK ANTREAN) 🔥
        if yt_id in channel_cooldowns:
            if time.time() < channel_cooldowns[yt_id]:
                sisa_menit = max(1, int((channel_cooldowns[yt_id] - time.time()) / 60))
                
                with db_lock:
                    for d in active_tasks:
                        if d['id'] == task_id:
                            d['status'] = f"Antrean Ditunda (Cooldown YT {sisa_menit} mnt) ⏳"
                save_tasks_db()
                
                render_queue.put(task)
                render_queue.task_done()
                time.sleep(5) 
                continue
            else:
                del channel_cooldowns[yt_id]
                
        temp_files = [
            os.path.join(BASE_UPLOAD, f"temp_a_{task_id}.mp3"),
            os.path.join(BASE_UPLOAD, f"temp_c_{task_id}.txt"),
            os.path.join(BASE_UPLOAD, f"temp_v_{task_id}.mp4"),
            os.path.join(BASE_UPLOAD, f"loop_{task_id}.txt"),
            os.path.join(BASE_DIR, f"static/final_{task_id}.mp4"),
        ]
        try:
            if not wait_for_resources(task_id): 
                raise Exception("Dibatalkan")
                
            with db_lock:
                for d in active_tasks:
                    if d['id'] == task_id: d['status'] = "Meracik Aset Gallery... ⚙️"
            save_tasks_db()

            audio_paths = get_all_audios(yt_id)
            if not audio_paths: raise Exception("Gallery Audio Kosong!")
            
            mp3_req = int(task.get('mp3_per_video', 5))
            mp3_count = min(mp3_req, len(audio_paths))
            selected_audios = audio_paths[:mp3_count] 

            track_schedule = []
            current_sec = 0.0

            base_audio = os.path.join(BASE_UPLOAD, f"temp_a_{task_id}.mp3")
            c_txt = os.path.join(BASE_UPLOAD, f"temp_c_{task_id}.txt")
            with open(c_txt, 'w', encoding='utf-8') as f:
                for ap in selected_audios:
                    safe_path = os.path.abspath(ap).replace('\\', '/')
                    f.write(f"file '{safe_path}'\n")
                    
                    probe = subprocess.run([get_ffprobe_path(), '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', ap], capture_output=True, text=True)
                    try: dur = float(probe.stdout.strip())
                    except: dur = 0.0
                    
                    title = os.path.splitext(os.path.basename(ap))[0]
                    
                    track_schedule.append({
                        'title': title,
                        'path': safe_path, 
                        'start': current_sec,
                        'end': current_sec + dur,
                        'duration': dur
                    })
                    current_sec += dur

            subprocess.run([get_ffmpeg_path(), '-y', '-threads', '2', '-f', 'concat', '-safe', '0', '-i', c_txt, '-c', 'copy', base_audio], check=True)

            probe = subprocess.run([
                get_ffprobe_path(), '-v', 'error', '-show_entries', 'format=duration', 
                '-of', 'default=noprint_wrappers=1:nokey=1', base_audio
            ], capture_output=True, text=True, check=True)
            base_duration_sec = float(probe.stdout.strip())
            
            if base_duration_sec <= 0: raise Exception("Durasi audio tidak valid!")

            channel_data = next((c for c in database_channel if c['yt_id'] == yt_id), None)
            ch_name = channel_data['name'] if channel_data else "KeiBot FM"

            bg_count = int(task.get('bg_count', 1))
            bg_paths = get_multi_backgrounds(yt_id, count=bg_count)
            if not bg_paths: raise Exception("Gallery Background Kosong!")

            preset = task.get('vis_preset')
            allowed_presets = task.get('vis_presets_allowed', [])
            vis_mode = task.get('vis_mode')
            if vis_mode == 'random' or preset == 'random':
                preset = get_random_preset(allowed_presets)
            elif vis_mode == 'smart':
                smart_preset = get_smart_preset(base_audio)
                if smart_preset:
                    preset = smart_preset
            if not isinstance(preset, dict):
                preset = {"color_bot": "#00d4ff", "color_top": "#7c5cfc", "color_part": "#ffffff", "pos_x": 50, "pos_y": 85, "width_pct": 60, "max_height": 40, "idle_height": 5, "bar_count": 64, "reactivity": 0.66, "spacing": 3, "part_amount": 3, "part_speed": 1.0, "effect_type": "spectrum", "use_beat_pulse": False, "particle_type": "sparkle", "fade_duration": 0, "use_watermark": False, "wm_text": "", "wm_color": "#ffffff", "wm_font": "M", "wm_size": 24, "wm_position": "bl", "wm_move": "none", "use_tracklist": False, "tl_font": "M", "tl_size": "medium", "tl_position": "tr", "tl_bg": "dark", "tl_title": "PLAYLIST"}

            preset['yt_id'] = yt_id 
            preset['use_floating_card'] = task.get('use_floating_card', False)
            preset['track_schedule'] = track_schedule
            preset['channel_name'] = ch_name

            base_video = os.path.join(BASE_UPLOAD, f"temp_v_{task_id}.mp4")
            final_video = os.path.join(BASE_DIR, f"static/final_{task_id}.mp4")

            if stop_flags.get(task_id): raise Exception("Dibatalkan")
            with db_lock:
                for d in active_tasks:
                    if d['id'] == task_id: d['status'] = "Rendering Base FFmpeg... ⚡"
            save_tasks_db()

            render_video_core(task_id, base_audio, bg_paths, base_video, base_duration_sec, preset)
            if stop_flags.get(task_id): raise Exception("Dibatalkan")

            # ── SMART CUT: potong & acak ulang chunk video ──
            smart_cut = task.get('smart_cut', False)
            cut_duration = float(task.get('cut_duration', 5))
            cut_remainder = task.get('cut_remainder', 'end')
            cut_use_remainder = task.get('cut_use_remainder', True)
            if smart_cut and cut_duration > 0 and base_duration_sec > cut_duration:
                with db_lock:
                    for d in active_tasks:
                        if d['id'] == task_id: d['status'] = f"Smart Cut {cut_duration}s... ✂️"
                save_tasks_db()

                full_chunks = int(base_duration_sec // cut_duration)
                remainder = base_duration_sec - (full_chunks * cut_duration)

                # segmentasi menggunakan FFmpeg
                seg_dir = os.path.join(BASE_UPLOAD, f"seg_{task_id}")
                os.makedirs(seg_dir, exist_ok=True)
                seg_pattern = os.path.join(seg_dir, "chunk_%03d.mp4")
                subprocess.run([
                    get_ffmpeg_path(), '-y', '-i', base_video,
                    '-c', 'copy', '-map', '0',
                    '-f', 'segment', '-segment_time', str(cut_duration),
                    '-reset_timestamps', '1',
                    seg_pattern
                ], check=True, capture_output=True)

                # kumpulkan chunk files
                chunks = sorted([os.path.join(seg_dir, f) for f in os.listdir(seg_dir) if f.endswith('.mp4')],
                                key=lambda x: int(x.split('_')[-1].split('.')[0]))
                # potong sesuai full_chunks (abaikan chunk kelebihan)
                chunks = chunks[:full_chunks]

                if len(chunks) >= 2:
                    # acak urutan chunk
                    random.shuffle(chunks)

                    # siapkan concat file
                    smart_txt = os.path.join(BASE_UPLOAD, f"smart_{task_id}.txt")
                    with open(smart_txt, 'w', encoding='utf-8') as f:
                        for ch in chunks:
                            f.write(f"file '{os.path.abspath(ch).replace(chr(92), '/')}'\n")

                    # sisipkan remainder (jika diaktifkan)
                    if cut_use_remainder and remainder > 0.5:
                        # ekstrak remainder dari base_video
                        rem_video = os.path.join(BASE_UPLOAD, f"rem_{task_id}.mp4")
                        subprocess.run([
                            get_ffmpeg_path(), '-y', '-i', base_video,
                            '-ss', str(full_chunks * cut_duration), '-t', str(remainder),
                            '-c', 'copy', rem_video
                        ], check=True, capture_output=True)

                        if cut_remainder == 'middle':
                            # sisipkan di tengah
                            lines = open(smart_txt).readlines()
                            mid = len(lines) // 2
                            lines.insert(mid, f"file '{os.path.abspath(rem_video).replace(chr(92), '/')}'\n")
                            with open(smart_txt, 'w') as f: f.writelines(lines)
                        elif cut_remainder == 'random':
                            # sisipkan di posisi acak
                            lines = open(smart_txt).readlines()
                            pos = random.randint(0, len(lines))
                            lines.insert(pos, f"file '{os.path.abspath(rem_video).replace(chr(92), '/')}'\n")
                            with open(smart_txt, 'w') as f: f.writelines(lines)
                        else:
                            # end - tambah di akhir
                            with open(smart_txt, 'a') as f:
                                f.write(f"file '{os.path.abspath(rem_video).replace(chr(92), '/')}'\n")

                    # concat ulang
                    smart_video = os.path.join(BASE_UPLOAD, f"smart_{task_id}.mp4")
                    subprocess.run([
                        get_ffmpeg_path(), '-y', '-threads', '2', '-f', 'concat', '-safe', '0',
                        '-i', smart_txt, '-c', 'copy', smart_video
                    ], check=True, capture_output=True)

                    # ganti base_video dengan hasil smart cut
                    shutil.move(smart_video, base_video)
                    base_duration_sec = full_chunks * cut_duration + (remainder if remainder > 0.5 else 0)

                # cleanup segment files
                shutil.rmtree(seg_dir, ignore_errors=True)

            target_hours = float(task.get('target_duration_hours', 1))
            target_sec = target_hours * 3600
            
            loop_count = math.ceil(target_sec / base_duration_sec)

            if loop_count > 1:
                with db_lock:
                    for d in active_tasks:
                        if d['id'] == task_id: d['status'] = f"Auto-Looping {loop_count}x ke {target_hours} Jam... 🚀"
                save_tasks_db()

                loop_txt = os.path.join(BASE_UPLOAD, f"loop_{task_id}.txt")
                with open(loop_txt, 'w', encoding='utf-8') as f:
                    for _ in range(loop_count):
                        safe_path_vid = os.path.abspath(base_video).replace('\\', '/')
                        f.write(f"file '{safe_path_vid}'\n")

                if stop_flags.get(task_id): raise Exception("Dibatalkan")
                subprocess.run([
                    get_ffmpeg_path(), '-y', '-threads', '2', '-f', 'concat', '-safe', '0', '-i', loop_txt, 
                    '-c', 'copy', '-t', str(target_sec), final_video
                ], check=True)
            else:
                if stop_flags.get(task_id): raise Exception("Dibatalkan")
                subprocess.run([
                    get_ffmpeg_path(), '-y', '-i', base_video, '-c', 'copy', '-t', str(target_sec), final_video
                ], check=True)

            if channel_data:
                creds_list = channel_data.get('creds_list', [channel_data.get('creds_json')])
                upload_berhasil = False
                pesan_error = "Token API Tidak Ditemukan/Kosong!" 
                
                for index_kunci, cred_str in enumerate(creds_list):
                    if not cred_str: continue
                    try:
                        creds = Credentials.from_authorized_user_info(json.loads(cred_str))
                        if creds.expired and creds.refresh_token: 
                            creds.refresh(Request())
                            
                        youtube = build('youtube', 'v3', credentials=creds)
                        try: sch_obj = datetime.strptime(task['publish_date'], "%Y-%m-%d %H:%M")
                        except: raise Exception("Format tanggal salah")
                        
                        raw_tags = task.get('tags', '')
                        clean_tags = raw_tags.replace('#', '').replace('<', '').replace('>', '').replace('"', '')
                        temp_tags = [t.strip() for t in clean_tags.split(',') if t.strip()]
                        
                        tags_list = []
                        char_count = 0
                        for t in temp_tags:
                            if char_count + len(t) <= 400:
                                tags_list.append(t)
                                char_count += len(t) + 1
                        
                        if not tags_list: tags_list = ['wavepush']
                        
                        body = {
                            'snippet': {'title': task['title'], 'description': task.get('description', ''), 'tags': tags_list, 'categoryId': '10'},
                            'status': {'privacyStatus': task.get('privacy', 'public')}
                        }
                        if sch_obj > datetime.now():
                            wib = ZoneInfo("Asia/Jakarta")
                            sch_aware = sch_obj.replace(tzinfo=wib)
                            sch_utc = sch_aware.astimezone(timezone.utc)
                            body['status']['publishAt'] = sch_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
                            body['status']['privacyStatus'] = 'private'
                            
                        media = MediaFileUpload(final_video, chunksize=1024*1024*5, resumable=True)
                        req = youtube.videos().insert(part=','.join(body.keys()), body=body, media_body=media)
                        resp = None
                        
                        max_retries = 5
                        retry_count = 0
                        
                        while resp is None:
                            if stop_flags.get(task_id): raise Exception("Dibatalkan")
                            try:
                                status, resp = req.next_chunk()
                                if status:
                                    with db_lock:
                                        for d in active_tasks:
                                            if d['id'] == task_id: d['status'] = f"Mengunggah (Key {index_kunci+1})... {int(status.progress()*100)}% 🚀"
                                    save_tasks_db()
                                retry_count = 0 
                            except HttpError as e:
                                if e.resp.status < 500:
                                    raise e
                                else:
                                    retry_count += 1
                                    if retry_count > max_retries: 
                                        raise Exception("Server YouTube Down/Timeout setelah 5x percobaan.")
                                    with db_lock:
                                        for d in active_tasks:
                                            if d['id'] == task_id: d['status'] = f"Koneksi Sinyal Lemah, Auto-Retry ({retry_count}/{max_retries})... 🔌"
                                    save_tasks_db()
                                    time.sleep(10)
                            except Exception as e:
                                retry_count += 1
                                if retry_count > max_retries: 
                                    raise Exception("Koneksi VPS Putus setelah dicoba 5x berturut-turut.")
                                with db_lock:
                                    for d in active_tasks:
                                        if d['id'] == task_id: d['status'] = f"Koneksi VPS Putus, Auto-Retry ({retry_count}/{max_retries})... 🔌"
                                save_tasks_db()
                                time.sleep(10)
                        
                        video_id = resp.get('id')
                        
                        thumb_path = get_and_consume_thumbnail(yt_id)
                        if thumb_path and os.path.exists(thumb_path):
                            try:
                                with db_lock:
                                    for d in active_tasks:
                                        if d['id'] == task_id: d['status'] = "Memasang Thumbnail... 🖼️"
                                save_tasks_db()
                                youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(thumb_path)).execute()
                                try:
                                    os.remove(thumb_path)
                                except Exception:
                                    pass
                            except: pass
                                
                        try:
                            if task.get('playlist_id'):
                                youtube.playlistItems().insert(part='snippet', body={'snippet': {'playlistId': task['playlist_id'], 'resourceId': {'kind': 'youtube#video', 'videoId': video_id}}}).execute()
                        except: pass
                        move_to_history(task_id, f"Tayang! ✅ <a href='https://youtu.be/{video_id}' target='_blank'>[Lihat]</a>")
                        upload_berhasil = True
                        break
                        
                    except HttpError as e:
                        try:
                            err_info = json.loads(e.content.decode('utf-8'))
                            reason = err_info['error']['errors'][0]['reason']
                        except:
                            reason = str(e)
                            
                        if "quotaExceeded" in reason:
                            pesan_error = f"Limit Kuota Harian API Habis!"
                            continue 
                        elif "uploadLimitExceeded" in reason:
                            pesan_error = "Limit Upload Harian Channel Tercapai!"
                            channel_cooldowns[yt_id] = time.time() + (3600 * 24)
                            break
                        elif "rateLimitExceeded" in reason:
                            pesan_error = "Rate Limit (Terlalu Cepat) - Auto Cooldown 30 Menit"
                            channel_cooldowns[yt_id] = time.time() + 1800
                            break
                        else:
                            pesan_error = f"Ditolak YT: {reason}"
                            break
                    except Exception as e:
                        err_str = str(e).lower()
                        if "invalid_grant" in err_str or "expired" in err_str or "revoked" in err_str:
                            pesan_error = "Sesi Kedaluwarsa (Tautkan Ulang!)"
                            channel_cooldowns[yt_id] = time.time() + (3600 * 24)
                        elif "timeout" in err_str or "connection" in err_str or "broken" in err_str:
                            pesan_error = "Koneksi VPS Putus/Timeout"
                        else:
                            pesan_error = f"Error: {str(e)[:40]}"
                        break
                        
                if not upload_berhasil:
                    if "API Habis" in pesan_error:
                        channel_cooldowns[yt_id] = time.time() + (3600 * 24)
                    raise Exception(pesan_error)
            else:
                move_to_history(task_id, f"Render Selesai ✅ <a href='/static/final_{task_id}.mp4' target='_blank'>[Download]</a>")
        
        except Exception as e:
            err_msg = str(e)
            if "Limit" in err_msg or "Cooldown" in err_msg or "Habis" in err_msg:
                with db_lock:
                    for d in active_tasks:
                        if d['id'] == task_id:
                            d['status'] = f"Gagal Upload, Antre Ulang ({err_msg}) ⏳"
                save_tasks_db()
                render_queue.put(task)
            else:
                move_to_history(task_id, f"Gagal ❌ ({err_msg})")
        finally:
            for path in temp_files:
                try: os.remove(path)
                except: pass
            stop_flags.pop(task_id, None)
            render_queue.task_done()

threading.Thread(target=background_worker, daemon=True).start()

# ==========================================
# 📊 API ENDPOINTS
# ==========================================
@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/notifications', methods=['GET'])
def get_notifications():
    return jsonify(system_notifications)

@app.route('/api/notifications/clear', methods=['POST'])
def clear_notifications():
    global system_notifications
    with db_lock:
        system_notifications.clear()
    return jsonify({"status": "success"})

@app.route('/api/get_dashboard_stats')
def get_dashboard_stats():
    sys = get_system_stats()
    return jsonify({
        "channels": len(database_channel), "active_tasks": len(active_tasks), "history_tasks": len(history_tasks),
        "sys_cpu": sys["cpu"], "sys_ram_pct": sys["ram_pct"], "sys_ram_text": f"{sys['ram_used']}GB / {sys['ram_total']}GB"
    })

@app.route('/api/get_youtube_analytics')
def get_youtube_analytics():
    data = []
    for c in database_channel:
        views, subs, videos = 0, 0, 0
        try:
            creds_list = c.get('creds_list', [c.get('creds_json')])
            if creds_list and creds_list[0]:
                creds = Credentials.from_authorized_user_info(json.loads(creds_list[0]))
                if creds.expired and creds.refresh_token: creds.refresh(Request())
                youtube = build('youtube', 'v3', credentials=creds)
                res = youtube.channels().list(part="statistics", id=c['yt_id']).execute()
                if res.get('items'):
                    stats = res['items'][0]['statistics']
                    views = int(stats.get('viewCount', 0))
                    subs = int(stats.get('subscriberCount', 0))
                    videos = int(stats.get('videoCount', 0))
        except Exception as e:
            pass
        data.append({"yt_id": c["yt_id"], "name": c["name"], "views": views, "subs": subs, "watch_hours": 0, "videos": videos})
    return jsonify(data)

@app.route('/api/get_schedule')
def get_schedule(): return jsonify({"active": active_tasks, "history": history_tasks})

@app.route('/api/clear_history', methods=['POST'])
def clear_history():
    global history_tasks
    with db_lock: history_tasks.clear()
    save_tasks_db()
    return jsonify({"status": "success", "message": "Riwayat dibersihkan!"})

@app.route('/api/get_channels')
def get_channels():
    safe_c = [{"id": c["id"], "name": c["name"], "yt_id": c["yt_id"], "thumbnail": c["thumbnail"], "status": c["status"], "title_bank": c.get("title_bank", [])} for c in database_channel]
    return jsonify(safe_c)

@app.route('/api/delete_channel', methods=['POST'])
def delete_channel():
    yt_id = request.form.get('yt_id')
    global database_channel
    database_channel = [c for c in database_channel if c['yt_id'] != yt_id]
    save_channels(database_channel)
    return jsonify({"status": "success", "message": "Channel dihapus!"})

# --- PRESET API ---
@app.route('/api/save_preset', methods=['POST'])
def save_preset():
    data = request.json
    try:
        presets = {}
        if os.path.exists(PRESETS_FILE):
            with open(PRESETS_FILE, 'r') as f:
                try: presets = json.load(f)
                except: pass
        presets.update(data)
        with open(PRESETS_FILE, 'w') as f: json.dump(presets, f, indent=4)
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error", "message": str(e)})

@app.route('/api/get_presets', methods=['GET'])
def get_presets():
    if os.path.exists(PRESETS_FILE):
        with open(PRESETS_FILE, 'r') as f:
            try: 
                return jsonify(json.load(f))
            except: 
                pass
    return jsonify({})

@app.route('/api/delete_preset', methods=['POST'])
def delete_preset():
    data = request.json
    preset_name = data.get('name')
    try:
        if os.path.exists(PRESETS_FILE):
            with open(PRESETS_FILE, 'r') as f:
                presets = json.load(f)
                
            if preset_name in presets:
                del presets[preset_name]
                
                with open(PRESETS_FILE, 'w') as f: 
                    json.dump(presets, f, indent=4)
                    
                return jsonify({"status": "success"})
                
        return jsonify({"status": "error", "message": "Preset tidak ditemukan"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

# ============================================================
# 🖼️ GALLERY ENDPOINTS
# ============================================================
@app.route('/api/get_asset_counts')
def get_asset_counts():
    yt_id = request.args.get('yt_id')
    if not yt_id: return jsonify({"audios": 0, "backgrounds": 0, "thumbnails": 0})
    def count_files(sub):
        path = get_channel_folder(yt_id, sub)
        return len([f for f in os.listdir(path) if os.path.isfile(os.path.join(path, f))])
    return jsonify({"audios": count_files("audios"), "backgrounds": count_files("backgrounds"), "thumbnails": count_files("thumbnails")})

@app.route('/api/get_gallery', methods=['GET'])
def get_gallery():
    yt_id = request.args.get('yt_id')
    if not yt_id: return jsonify({"audio": [], "background": [], "thumbnails": []})
    def get_files_data(sub):
        path = get_channel_folder(yt_id, sub)
        res = []
        if os.path.exists(path):
            for f in os.listdir(path):
                fp = os.path.join(path, f)
                if os.path.isfile(fp):
                    size_mb = round(os.path.getsize(fp) / (1024*1024), 2)
                    res.append({"name": f, "size": f"{size_mb} MB"})
        return res
    return jsonify({
        "audio":      get_files_data("audios"),
        "background": get_files_data("backgrounds"),
        "thumbnails": get_files_data("thumbnails")
    })

@app.route('/api/get_audio_info', methods=['GET'])
def get_audio_info():
    yt_id = request.args.get('yt_id')
    if not yt_id: return jsonify([])
    # cek cache
    now = time.time()
    cached = _audio_info_cache.get(yt_id)
    if cached and (now - cached['ts']) < AUDIO_CACHE_TTL:
        return jsonify(cached['data'])
    # baca folder & hitung durasi via ffprobe
    path = get_channel_folder(yt_id, "audios")
    result = []
    if os.path.exists(path):
        for f in sorted(os.listdir(path)):
            if f.lower().endswith(('.mp3', '.wav')):
                fp = os.path.join(path, f)
                probe = subprocess.run([get_ffprobe_path(), '-v', 'error', '-show_entries', 'format=duration',
                    '-of', 'default=noprint_wrappers=1:nokey=1', fp], capture_output=True, text=True)
                try: dur = float(probe.stdout.strip())
                except: dur = 0.0
                title = os.path.splitext(f)[0]
                result.append({"name": f, "title": title, "duration": dur})
    _audio_info_cache[yt_id] = {"data": result, "ts": now}
    return jsonify(result)

@app.route('/api/upload_gallery', methods=['POST'])
def upload_gallery():
    yt_id  = request.form.get('yt_id', '').strip()
    g_type = request.form.get('type',  '').strip()

    if not yt_id:
        return jsonify({"status": "error", "message": "yt_id tidak boleh kosong!"}), 400
    if not g_type:
        return jsonify({"status": "error", "message": "type tidak boleh kosong!"}), 400

    folder_name = resolve_folder(g_type)
    folder      = get_channel_folder(yt_id, folder_name)

    files = (request.files.getlist('files[]')
             or request.files.getlist('files')
             or request.files.getlist('file')
             or list(request.files.values()))

    if not files:
        return jsonify({"status": "error", "message": "Tidak ada file yang diterima!"}), 400

    saved, errors = 0, []
    for f in files:
        if not f or not f.filename:
            continue
        try:
            safe_name = os.path.basename(f.filename)
            dest = os.path.join(folder, safe_name)
            f.save(dest)
            saved += 1
        except Exception as e:
            errors.append(f"{f.filename}: {str(e)}")

    if saved == 0:
        return jsonify({"status": "error", "message": "Tidak ada file yang berhasil disimpan. " + "; ".join(errors)}), 500

    msg = f"{saved} file berhasil diupload ke '{folder_name}'"
    if errors:
        msg += f" ({len(errors)} gagal: {'; '.join(errors[:3])})"
    return jsonify({"status": "success", "message": msg})

@app.route('/api/delete_gallery_file', methods=['POST'])
def delete_gallery_file():
    yt_id  = request.form.get('yt_id', '').strip()
    g_type = request.form.get('type',  '').strip()
    name   = request.form.get('name',  '').strip()

    folder_name = resolve_folder(g_type)
    path = os.path.join(get_channel_folder(yt_id, folder_name), os.path.basename(name))

    if os.path.exists(path):
        os.remove(path)
        return jsonify({"status": "success", "message": "File dihapus!"})
    return jsonify({"status": "error", "message": f"File tidak ditemukan: {path}"})

# ============================================================
# 📝 TITLE BANK ENDPOINT
# ============================================================
@app.route('/api/upload_title_bank', methods=['POST'])
def upload_title_bank():
    yt_id = (request.form.get('yt_id') or request.args.get('yt_id') or '').strip()
    txt_file = request.files.get('txt_file') or request.files.get('file')

    if not yt_id:
        return jsonify({"status": "error", "message": "yt_id tidak ditemukan. Pastikan channel sudah dipilih."}), 400
    if not txt_file:
        return jsonify({"status": "error", "message": "File .txt tidak ditemukan dalam request."}), 400

    try:
        raw_bytes = txt_file.read()
        try:   content = raw_bytes.decode('utf-8')
        except: content = raw_bytes.decode('latin-1', errors='ignore')

        lines = [line.strip() for line in content.split('\n') if line.strip()]
        if not lines:
            return jsonify({"status": "error", "message": "File .txt kosong atau tidak ada baris valid."}), 400

        global database_channel
        channel_found = False
        for c in database_channel:
            if c['yt_id'] == yt_id:
                existing = c.get('title_bank', [])
                merged   = list(dict.fromkeys(existing + lines))
                c['title_bank'] = merged
                channel_found = True
                save_channels(database_channel)
                return jsonify({
                    "status":  "success",
                    "message": f"{len(lines)} judul diimport! Total bank: {len(merged)} judul.",
                    "total":   len(merged),
                })

        if not channel_found:
            return jsonify({"status": "error", "message": f"Channel dengan yt_id '{yt_id}' tidak ditemukan di database."}), 404

    except Exception as e:
        return jsonify({"status": "error", "message": f"Gagal memproses file: {str(e)}"}), 500

@app.route('/api/get_playlists', methods=['GET'])
def get_playlists():
    yt_id = request.args.get('yt_id')
    if not yt_id: return jsonify([])
    channel = next((c for c in database_channel if c['yt_id'] == yt_id), None)
    if not channel: return jsonify([])
    try:
        creds = get_fresh_credentials(channel)
        youtube = build('youtube', 'v3', credentials=creds)
        res = youtube.playlists().list(part="snippet", mine=True, maxResults=50).execute()
        return jsonify([{"id": p['id'], "title": p['snippet']['title']} for p in res.get('items', [])])
    except: return jsonify([])

@app.route('/api/stop_task/<int:task_id>', methods=['POST'])
def stop_task(task_id):
    stop_flags[task_id] = True
    return jsonify({"status": "success", "message": "Dihentikan!"})

@app.route('/api/check_secret')
def check_secret():
    try: return jsonify({"exists": os.path.exists(CLIENT_SECRETS_FILE)})
    except: return jsonify({"exists": False})

@app.route('/api/upload_secret', methods=['POST'])
def upload_secret():
    try:
        file = request.files.get('secret_file')
        if file and file.filename.endswith('.json'):
            file.save(CLIENT_SECRETS_FILE)
            return jsonify({"status": "success", "message": "API Key diunggah!"})
        return jsonify({"status": "error", "message": "Harus .json!"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Izin ditolak server: {str(e)}"})

@app.route('/api/generate_tv_link')
def generate_tv_link():
    if not os.path.exists(CLIENT_SECRETS_FILE): return jsonify({"auth_url": "", "error": "File client_secret.json belum ada!"})
    return jsonify({"auth_url": f"http://{request.host}/device_login"})

@app.route('/device_login')
def device_login():
    if not os.path.exists(CLIENT_SECRETS_FILE): return "File rahasia tidak ditemukan!"
    with open(CLIENT_SECRETS_FILE, 'r') as f:
        secret_data = json.load(f); client_config = secret_data.get('installed', secret_data.get('web', {})); client_id = client_config.get('client_id')
    res = requests.post('https://oauth2.googleapis.com/device/code', data={'client_id': client_id, 'scope': ' '.join(SCOPES)}).json()
    if 'error' in res: return f"Error Google: {res['error']}"
    html = f"""
    <html><head><title>Aktivasi YouTube</title>
    <style>
        body {{ font-family: 'Segoe UI', Arial; text-align: center; background: #eef2f6; color: #1e293b; padding-top: 10vh; }}
        .box {{ background: #ffffff; width: 550px; margin: auto; padding: 40px; border-radius: 16px; box-shadow: 0 10px 25px rgba(0,0,0,0.05); border: 1px solid #e2e8f0; }}
        .step {{ text-align: left; margin-bottom: 25px; font-size: 14px; color: #64748b; font-weight:600; }}
        .input-group {{ display: flex; margin-top: 10px; }}
        .input-group input {{ flex: 1; padding: 15px; font-size: 16px; font-weight: bold; background: #f8fafc; color: #10b981; border: 1px solid #e2e8f0; border-radius: 8px 0 0 8px; text-align: center; outline:none; }}
        .input-group button {{ padding: 15px 25px; font-size: 14px; font-weight: bold; background: #10b981; color: white; border: none; border-radius: 0 8px 8px 0; cursor: pointer; transition: 0.3s; }}
    </style></head><body>
        <div class="box">
            <h2 style="margin-top:0;">🔗 Tautkan Channel Baru</h2>
            <div class="step"><b>Langkah 1:</b> Copy link ini dan Paste di browser target:
                <div class="input-group"><input type="text" id="glink" value="{res['verification_url']}" readonly><button onclick="document.getElementById('glink').select();document.execCommand('copy');">Copy Link</button></div>
            </div>
            <div class="step"><b>Langkah 2:</b> Masukkan Kode Rahasia ini:
                <div class="input-group"><input type="text" id="gcode" value="{res['user_code']}" readonly><button onclick="document.getElementById('gcode').select();document.execCommand('copy');">Copy Kode</button></div>
            </div>
            <div id="status" style="margin-top:30px; font-weight:bold;">⏳ Menunggu Anda memasukkan kode...</div>
        </div>
        <script>
            function poll() {{ fetch('/api/poll_device_token', {{ method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{device_code: '{res['device_code']}'}}) }}).then(r => r.json()).then(data => {{ if(data.status === 'success') {{ document.getElementById('status').innerHTML = "🎉 Berhasil! Mengalihkan..."; setTimeout(() => {{ window.location.href = '/'; }}, 2000); }} else if(data.status === 'pending') {{ setTimeout(poll, data.interval || 5000); }} }}); }}
            setTimeout(poll, 5000);
        </script>
    </body></html>
    """
    return html

@app.route('/api/poll_device_token', methods=['POST'])
def poll_device_token():
    device_code = request.json.get('device_code')
    with open(CLIENT_SECRETS_FILE, 'r') as f:
        s_data = json.load(f); conf = s_data.get('installed', s_data.get('web', {})); c_id = conf.get('client_id'); c_sec = conf.get('client_secret')
    res = requests.post('https://oauth2.googleapis.com/token', data={'client_id': c_id, 'client_secret': c_sec, 'device_code': device_code, 'grant_type': 'urn:ietf:params:oauth:grant-type:device_code'}).json()
    if 'error' in res:
        err = res['error']
        if err == 'authorization_pending': return jsonify({"status": "pending", "interval": 5000})
        elif err == 'slow_down': return jsonify({"status": "pending", "interval": 10000})
        else: return jsonify({"status": "error", "error": err})
    creds = Credentials(token=res['access_token'], refresh_token=res.get('refresh_token'), token_uri='https://oauth2.googleapis.com/token', client_id=c_id, client_secret=c_sec, scopes=SCOPES)
    youtube = build('youtube', 'v3', credentials=creds); chan_res = youtube.channels().list(part="snippet", mine=True).execute()
    if chan_res['items']:
        item = chan_res['items'][0]; global database_channel
        c_idx = next((i for i, c in enumerate(database_channel) if c['yt_id'] == item['id']), None)
        if c_idx is None:
            new_c = {"id": len(database_channel)+1, "name": item['snippet']['title'], "yt_id": item['id'], "thumbnail": item['snippet']['thumbnails']['default']['url'], "status": "Connected 🟢 (1 Key)", "creds_list": [creds.to_json()]}
            database_channel.append(new_c)
        else:
            if 'creds_list' not in database_channel[c_idx]:
                database_channel[c_idx]['creds_list'] = [database_channel[c_idx].get('creds_json', '')]
            if creds.to_json() not in database_channel[c_idx]['creds_list']:
                database_channel[c_idx]['creds_list'].append(creds.to_json())
            database_channel[c_idx]['status'] = f"Connected 🟢 ({len(database_channel[c_idx]['creds_list'])} Keys)"
        save_channels(database_channel)
    return jsonify({"status": "success"})

# --- BATCH CREATOR ---
@app.route('/api/batch_create', methods=['POST'])
def batch_create():
    data = request.json
    yt_id = data.get('yt_id')
    count = data.get('count', 1)
    titles = data.get('generated_titles', [])
    durations_array = data.get('target_durations_array', []) 
    
    try:
        base_date = datetime.strptime(data['start_date'], '%Y-%m-%dT%H:%M')
    except:
        return jsonify({"status": "error", "message": "Format tanggal salah"}), 400
        
    # 🔥 FIX 1: Konversi interval_days ke float secara eksplisit untuk mencegah TypeError pada timedelta
    interval_days = float(data.get('interval_days', 1))
        
    for i in range(count):
        t_id = int(time.time()) + i
        v_date = base_date + timedelta(days=i * interval_days)
        
        if i < len(durations_array):
            vid_duration = durations_array[i]
        else:
            vid_duration = data.get('target_duration_hours', 1)
            
        blueprint = {
            "id": t_id, "yt_id": yt_id, "title": titles[i] if i < len(titles) else f"Auto Video #{i+1}",
            "publish_date": v_date.strftime('%Y-%m-%d %H:%M'),
            "mp3_per_video": data.get('mp3_per_video', 5), 
            "bg_count": data.get('bg_count', 1), 
            "target_duration_hours": vid_duration,
            "vis_mode": data.get('vis_mode'), "vis_preset": data.get('vis_preset'),
            "vis_presets_allowed": data.get('vis_presets_allowed', []), "description": data.get('description', ''),
            "tags": data.get('tags', ''), "privacy": data.get('privacy', 'public'), "playlist_id": data.get('playlist_id', ''),
            "use_floating_card": data.get('use_floating_card', False),
            "smart_cut": data.get('smart_cut', False),
            "cut_duration": data.get('cut_duration', 5),
            "cut_remainder": data.get('cut_remainder', 'end'),
            "cut_use_remainder": data.get('cut_use_remainder', True)
        }
        with db_lock:
            active_tasks.append({"id": t_id, "title": blueprint['title'], "time": blueprint['publish_date'], "status": "In Factory Queue ⚙️", "type": "📺 VOD"})
        
        # Masukkan blueprint ke antrean in-memory worker
        render_queue.put(blueprint)
        
    # 🔥 FIX 2: Pindahkan save_tasks_db() ke LUAR perulangan 'for' 
    # Menghindari penulisan beruntun ke disk I/O yang menyebabkan VPS freeze/lag
    save_tasks_db()
    
    return jsonify({"status": "success", "message": f"{count} Video diproses!"})
    
@app.route('/uploads/<path:filename>')
def serve_uploads(filename):
    return send_from_directory(BASE_UPLOAD, filename)

if __name__ == '__main__':
    for t in active_tasks:
        if t['status'] == "In Factory Queue ⚙️" or "Rendering" in t['status']:
            t['status'] = "Dibatalkan (Server Restart) ⚠️"
            history_tasks.insert(0, t)
    active_tasks = [t for t in active_tasks if "Dibatalkan" not in t['status']]
    save_tasks_db()
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
