# 🎬 KeiBot Factory — Automation Studio

Sistem otomatisasi pembuatan & upload video musik (lofi/chill/ambient) ke YouTube secara batch. Dilengkapi dengan **8 efek visualizer**, **4 style particle**, sistem penjadwalan, multi-channel, dan auto-upload 24/7.

![Version](https://img.shields.io/badge/version-2.0 BATCH ENGINE-blue)
![Python](https://img.shields.io/badge/Python-3.8+-green)
![Flask](https://img.shields.io/badge/Flask-3.0.0-black)
![Platform](https://img.shields.io/badge/Platform-Linux%20VPS-orange)

---

## ✨ Fitur Utama

| Fitur | Deskripsi |
|---|---|
| 🎨 **Batch Creator** | Buat puluhan video sekaligus dengan satu kali setting |
| 🎵 **8 Efek Visualizer** | Spectrum, Circular, Waveform, Mirror, Neon Glow, Sunburst, Pixel 8-bit, Double Symmetric |
| ✨ **4 Particle Style** | Sparkle, Fireworks, Trail, Falling Petals/Stars |
| 💫 **Beat Pulse / Glow** | Efek flash overlay saat bass hit (bisa dikombinasi) |
| 📅 **Auto Scheduling** | Jadwalkan publish video ke YouTube sesuai tanggal & interval |
| 📺 **Multi-Channel** | Kelola banyak channel YouTube dalam 1 panel |
| 🎵 **Title Generator** | Generate judul otomatis dari bank kata |
| 🖼️ **Gallery Manager** | Upload & kelola audio (MP3), background (video/gambar), thumbnail |
| 📊 **Dashboard Analytics** | Pantau subscriber, watch hours, dan status monetisasi |
| 🔒 **PIN Security** | Sistem keamanan PIN untuk akses panel |

---

## 📦 Kebutuhan Sistem (VPS)

- **OS:** Ubuntu 20.04 / 22.04 / 24.04 (atau Debian-based)
- **RAM:** Minimal 2 GB (Recommended 4 GB+ untuk render video)
- **Root access** (sudo)
- **Koneksi internet** stabil

---

## 🚀 Cara Instalasi

Ada 2 cara instalasi. Pilih yang paling nyaman.

---

### ⚡ Cara 1 — Instalasi Otomatis (RECOMMENDED)

Cukup jalankan **1 perintah** di VPS, semua akan otomatis terinstal.

#### Langkah 1: Login ke VPS

Buka Terminal (Linux/Mac) atau PuTTY (Windows), lalu SSH ke VPS:

```bash
ssh root@IP_VPS_KAMU
```

#### Langkah 2: Jalankan Script Instalasi

```bash
bash <(curl -s https://raw.githubusercontent.com/USERNAME_KAMU/NAMA_REPO/main/install.sh)
```

> ⚠️ **Ganti** `USERNAME_KAMU/NAMA_REPO` dengan username GitHub dan nama repository kamu.

Tunggu proses instalasi selesai (±5-10 menit tergantung kecepatan VPS).

---

### 🔧 Cara 2 — Instalasi Manual (Step by Step)

Jika ingin memahami setiap langkah, ikuti panduan ini.

#### Langkah 1: Login ke VPS via SSH

```bash
ssh root@IP_VPS_KAMU
```

#### Langkah 2: Update Sistem & Install Dependensi

```bash
sudo apt-get update -y
sudo apt-get install -y ffmpeg python3-pip python3-venv git
```

#### Langkah 3: Clone Repository dari GitHub

```bash
cd /root
git clone https://github.com/USERNAME_KAMU/NAMA_REPO.git keibot-factory
cd keibot-factory
```

#### Langkah 4: Buat Virtual Environment & Install Library

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

#### Langkah 5: Buat Systemd Service (Agar Jalan 24/7)

```bash
cat <<EOF > /etc/systemd/system/keibot.service
[Unit]
Description=KeiBot Factory Web Panel
After=network.target

[Service]
User=root
WorkingDirectory=/root/keibot-factory
ExecStart=/root/keibot-factory/venv/bin/python /root/keibot-factory/app.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF
```

#### Langkah 6: Aktifkan Service

```bash
systemctl daemon-reload
systemctl enable keibot
systemctl restart keibot
```

#### Langkah 7: Cek Status

```bash
systemctl status keibot
```

Jika muncul **`active (running)`** berarti instalasi berhasil! ✅

---

## 🌐 Cara Akses Panel

Setelah instalasi selesai, buka browser dan akses:

```
http://IP_VPS_KAMU:5000
```

### Setup Pertama Kali

1. Buka `http://IP_VPS:5000` → akan muncul halaman **Setup PIN**
2. Buat PIN keamanan (simpan baik-baik!)
3. Login dengan PIN tersebut
4. Masuk ke menu **Settings** → tambahkan channel YouTube & upload `client_secrets.json`
5. Selesai! Panel siap digunakan

---

## 🔥 Jika Tidak Bisa Diakses (Firewall)

Jika browser tidak bisa membuka panel, kemungkinan port 5000 diblokir firewall. Buka dengan:

```bash
# Menggunakan UFW
sudo ufw allow 5000/tcp
sudo ufw reload

# Menggunakan iptables
sudo iptables -A INPUT -p tcp --dport 5000 -j ACCEPT
```

Juga pastikan di **panel provider VPS** (seperti contoh: DigitalOcean, Linode, Vultr, atau provider lain) firewall cloud-nya mengizinkan port 5000.

---

## 🔄 Cara Update ke Versi Baru

Jika ada update kode di GitHub, jalankan ini di VPS untuk meng-update:

```bash
cd /root/keibot-factory
git pull origin main
systemctl restart keibot
```

---

## 🎨 Panduan Efek Visualizer

KeiBot Factory dilengkapi **8 efek spectrum** dan **4 particle style** yang bisa dikombinasikan:

### Efek Spectrum (8 variasi)

| Efek | Kode | Deskripsi |
|---|---|---|
| ◉ Spectrum Bars | `spectrum` | Bar audio klasik ke atas/tengah (default) |
| ◎ Circular Spectrum | `circular` | Bar membentuk lingkaran radial |
| ∿ Waveform | `waveform` | Gelombang audio oscilloscope |
| ⬒ Mirror Spectrum | `mirror` | Bar dengan refleksi simetris atas-bawah |
| ✦ Neon Glow Bars | `neon_glow` | Bar dengan efek glow/bloom neon |
| ☀ Radial Sunburst | `sunburst` | Garis memancar dari pusat seperti matahari |
| ▦ Pixel Blocks | `pixel` | Blok 8-bit retro ala game klasik |
| ⇆ Double Symmetric | `double_symmetric` | Mirror kiri-kanan simetris |

### Particle Style (4 variasi)

| Efek | Kode | Deskripsi |
|---|---|---|
| ✦ Sparkle | `sparkle` | Partikel kecil default (default) |
| 🎆 Fireworks | `fireworks` | Ledakan kembang api saat beat drop |
| ☄ Trail | `trail` | Partikel dengan ekor jejak (shooting star) |
| ❅ Falling Petals | `petals` | Bintang jatuh perlahan (cocok untuk lofi/chill) |

### Overlay Tambahan

| Efek | Deskripsi |
|---|---|
| 💫 Beat Pulse / Glow | Flash overlay saat bass hit — bisa dikombinasi dengan efek apa saja |

> 💡 **Tips:** Total kombinasi = **8 spectrum × 4 particle × 2 (beat pulse)** = **64+ variasi visual!**

---

## 📁 Struktur Project

```
keibot-factory-main/
├── app.py                  # Backend Flask + VisualEngine (FFmpeg rendering)
├── install.sh              # Script instalasi otomatis
└── requirements.txt        # Daftar dependensi Python
├── static/
│   └── logo.png            # Logo aplikasi
└── templates/
    ├── index.html          # UI utama (Batch Creator, Visualizer, dll)
    ├── login.html          # Halaman login PIN
    └── setup.html          # Halaman setup PIN pertama kali
```

---

## 🛠️ Perintah Manajemen (Command Line)

| Perintah | Fungsi |
|---|---|
| `systemctl status keibot` | Cek status aplikasi |
| `systemctl restart keibot` | Restart aplikasi |
| `systemctl stop keibot` | Stop aplikasi |
| `systemctl start keibot` | Start aplikasi |
| `journalctl -u keibot -f` | Lihat log realtime |
| `journalctl -u keibot -n 50` | Lihat 50 baris log terakhir |

---

## ❓ Troubleshooting

<details>
<summary><b>🔴 Panel tidak bisa diakses</b></summary>

1. Cek status service: `systemctl status keibot`
2. Cek apakah port 5000 terbuka: `sudo ufw allow 5000/tcp`
3. Cek firewall di panel provider VPS
4. Lihat log error: `journalctl -u keibot -n 50`
</details>

<details>
<summary><b>🔴 Video gagal di-render</b></summary>

1. Pastikan FFmpeg terinstall: `ffmpeg -version`
2. Cek RAM tersedia (minimal 2GB saat render)
3. Upload background (video/gambar) di menu Gallery
4. Cek log: `journalctl -u keibot -f` lalu jalankan batch
</details>

<details>
<summary><b>🔴 Upload YouTube gagal</b></summary>

1. Pastikan `client_secrets.json` sudah diupload di Settings
2. Pastikan channel YouTube sudah diotorisasi
3. Pastikan jam dari VPS akurat: `timedatectl status`
4. Cek quota YouTube Data API di Google Cloud Console
</details>

<details>
<summary><b>🔴 Aplikasi selalu crash / restart terus</b></summary>

1. Lihat log: `journalctl -u keibot -n 100`
2. Cek apakah ada file korup setelah update
3. Reinstall library: `cd /root/keibot-factory && source venv/bin/activate && pip install -r requirements.txt`
4. Restart: `systemctl restart keibot`
</details>

---

## 📝 Lisensi

Project ini bersifat open-source untuk penggunaan pribadi.

---

## 🙏 Kredit

Dibuat dengan ❤️ untuk para kreator musik lofi/chill.

**KeiBot Factory v2.0 — BATCH ENGINE**
</content>
</invoke>