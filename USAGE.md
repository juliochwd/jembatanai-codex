# JembatanAI-Codex — Penggunaan Lengkap

**Last Updated**: 2026-03-26  
**Status**: Production Ready  
**Gateway**: `https://gateway.jembatanai.com`

---

## Daftar Isi

1. [Apa itu JembatanAI-Codex?](#apa-itu-jembatanai-codex)
2. [Arsitektur](#arsitektur)
3. [Daftar Model](#daftar-model)
4. [Setup di Windows](#setup-di-windows)
5. [Setup di Linux/macOS](#setup-di-linuxmacos)
6. [Cara Pakai Codex CLI](#cara-pakai-codex-cli)
7. [API Reference](#api-reference)
8. [Troubleshooting](#troubleshooting)
9. [FAQ](#faq)

---

## Apa itu JembatanAI-Codex?

JembatanAI-Codex memungkinkan Anda menggunakan **OpenAI Codex CLI** dengan **model AI gratis** (dari Kilo/OpenRouter) melalui gateway JembatanAI. Anda mendapatkan pengalaman Codex CLI yang sama seperti OpenAI, tapi menggunakan model gratis.

**Yang Anda dapatkan:**
- Codex CLI (coding agent) berjalan di komputer Anda
- Model AI gratis (tidak perlu langganan OpenAI)
- Tool calling (bash, file editing, dll)
- Streaming response real-time
- Multi-turn conversation dengan context

---

## Arsitektur

```
PC Anda (Codex CLI)
    │
    │  HTTPS (wire_api: "responses")
    ▼
gateway.jembatanai.com
    │
    │  API key validation + quota check
    ▼
Node.js Gateway (port 3000)
    │
    │  passthrough
    ▼
jembatanai-codex (port 4110)
    │
    │  model resolution (gpt-5.4 → kilo model)
    ▼
Kilo API (free models via OpenRouter)
```

**Endpoint utama:**
- `POST /v1/responses` — Codex CLI primary endpoint
- `POST /v1/chat/completions` — OpenAI Chat API
- `GET /v1/models` — Model discovery

---

## Daftar Model

Semua model **gratis** dan bisa dipakai langsung.

| Nama di Codex | Model Sebenarnya | Context | Keunggulan |
|---------------|-----------------|---------|------------|
| `gpt-5.4` | Xiaomi MiMo V2 Pro | 1M | Reasoning, default |
| `gpt-5.3` | Xiaomi MiMo Omni | 262K | Multimodal (gambar) |
| `gpt-4o` | Xiaomi MiMo V2 Pro | 1M | General purpose |
| `gpt-4o-mini` | MiniMax M2.5 | 204K | Cepat, ringan |
| `o1` | xAI Grok Code | 256K | Coding specialist |
| `o3-mini` | Arcee Trinity | 131K | Reliable, stabil |
| `codex` | Xiaomi MiMo V2 Pro | 1M | Default alias |

Ganti model di dalam Codex dengan mengetik `/model`.

---

## Setup di Windows

### Prasyarat

- **Node.js** (v18+) — download di https://nodejs.org
- **PowerShell** (sudah ada di Windows)

### One-Click Setup

Buka **PowerShell**, copy-paste ini:

```powershell
irm https://gateway.jembatanai.com/setup-codex.ps1 | iex
```

Script ini akan otomatis:
1. Install Codex CLI (`npm install -g @openai/codex`)
2. Logout dari OpenAI/ChatGPT (hapus `auth.json` yang konflik)
3. Set API key JembatanAI
4. Tulis konfigurasi `config.toml`
5. Set environment variable

### Setelah Setup

1. **Tutup PowerShell** yang dipakai untuk setup
2. **Buka PowerShell BARU** (agar env variable ter-load)
3. Ketik:

```powershell
codex
```

### Setup Manual (jika one-liner gagal)

**Step 1** — Install Codex CLI:
```powershell
npm install -g @openai/codex
```

**Step 2** — Logout dari OpenAI (PENTING!):
```powershell
codex logout
```

**Step 3** — Set API key:
```powershell
$apiKey = "gw-admin-SuG66BxPfKh3JzQUC9Rb-9zn9SvrQFYo5YBFhU6WC"
$env:JEMBATANAI_API_KEY = $apiKey
[Environment]::SetEnvironmentVariable("JEMBATANAI_API_KEY", $apiKey, "User")
```

**Step 4** — Tulis config (`~\.codex\config.toml`):
```powershell
@"
model = "gpt-5.4"
model_provider = "jembatanai"
model_reasoning_effort = "high"
approval_policy = "never"
sandbox_mode = "danger-full-access"

[model_providers.jembatanai]
name = "JembatanAI"
base_url = "https://gateway.jembatanai.com/v1"
env_key = "JEMBATANAI_API_KEY"
wire_api = "responses"

[notice]
hide_full_access_warning = true
hide_rate_limit_model_nudge = true
"@ | Set-Content "$env:USERPROFILE\.codex\config.toml" -Encoding UTF8
```

**Step 5** — Jalankan:
```powershell
codex
```

---

## Setup di Linux/macOS

### Prasyarat

- **Node.js** (v18+)
- **npm**

### Setup

```bash
# 1. Install Codex CLI
npm install -g @openai/codex

# 2. Logout dari OpenAI (PENTING!)
codex logout

# 3. Set API key
export JEMBATANAI_API_KEY="gw-admin-SuG66BxPfKh3JzQUC9Rb-9zn9SvrQFYo5YBFhU6WC"
echo 'export JEMBATANAI_API_KEY="gw-admin-SuG66BxPfKh3JzQUC9Rb-9zn9SvrQFYo5YBFhU6WC"' >> ~/.bashrc

# 4. Tulis config
cat > ~/.codex/config.toml << 'EOF'
model = "gpt-5.4"
model_provider = "jembatanai"
model_reasoning_effort = "high"
approval_policy = "never"
sandbox_mode = "danger-full-access"

[model_providers.jembatanai]
name = "JembatanAI"
base_url = "https://gateway.jembatanai.com/v1"
env_key = "JEMBATANAI_API_KEY"
wire_api = "responses"

[notice]
hide_full_access_warning = true
hide_rate_limit_model_nudge = true
EOF

# 5. Jalankan
codex
```

---

## Cara Pakai Codex CLI

### Memulai

```bash
codex                    # Mode interaktif
codex "buatkan fungsi python untuk sort"  # Langsung prompt
codex exec "ls -la"      # Non-interaktif
```

### Perintah Dasar di Dalam Codex

| Perintah | Fungsi |
|----------|--------|
| `/model` | Ganti model AI |
| `/status` | Lihat status session |
| `/compact` | Ringkas history (hemat token) |
| `/help` | Bantuan |
| `Ctrl+C` | Cancel operasi |
| `Ctrl+D` | Keluar |

### Contoh Penggunaan

```
> buatkan fungsi python untuk menghitung fibonacci
```

Codex akan:
1. Memahami permintaan
2. Menulis kode
3. Membuat file (dengan izin Anda)
4. Menjalankan test jika diminta

```
> refactor file utils.js untuk menggunakan ES6 class
```

Codex akan membaca file, menganalisis, dan membuat perubahan.

```
> cari bug di file server.py, jalankan testnya
```

Codex akan membaca kode, menemukan masalah, dan menjalankan test.

### Mengganti Model

Di dalam Codex, ketik `/model`, lalu pilih:

```
Available models:
  gpt-5.4      — Xiaomi MiMo V2 Pro (1M ctx, reasoning)
  gpt-4o-mini  — MiniMax M2.5 (204K ctx, fast)
  o1           — xAI Grok Code (256K ctx, coding)
  ...
```

---

## API Reference

Base URL: `https://gateway.jembatanai.com/v1`

### Authentication

Semua request perlu API key:

```
Authorization: Bearer gw-your-api-key-here
```

Atau via header:

```
x-api-key: gw-your-api-key-here
```

### POST /v1/responses

Endpoint utama untuk Codex CLI.

**Request:**
```json
{
  "model": "gpt-5.4",
  "input": [
    {"type": "message", "role": "user", "content": "Hello"}
  ],
  "max_output_tokens": 4096,
  "stream": false,
  "tools": [
    {
      "type": "function",
      "name": "shell",
      "description": "Run a shell command",
      "parameters": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"]
      }
    }
  ]
}
```

**Response:**
```json
{
  "id": "resp_abc123",
  "object": "response",
  "created_at": 1774531234,
  "model": "gpt-5.4",
  "status": "completed",
  "output": [
    {
      "type": "message",
      "role": "assistant",
      "content": [{"type": "output_text", "text": "Hello!"}]
    }
  ],
  "usage": {
    "input_tokens": 42,
    "output_tokens": 16,
    "total_tokens": 58
  }
}
```

### POST /v1/responses (Streaming)

Set `"stream": true` untuk SSE streaming:

```
event: response.created
data: {"type": "response.created", "response": {"id": "resp_...", ...}}

event: response.output_item.added
data: {"type": "response.output_item.added", ...}

event: response.output_text.delta
data: {"type": "response.output_text.delta", "delta": "Hello"}

event: response.completed
data: {"type": "response.completed", ...}
```

### POST /v1/chat/completions

Format OpenAI standar.

**Request:**
```json
{
  "model": "gpt-4o-mini",
  "messages": [
    {"role": "user", "content": "Hello"}
  ],
  "max_tokens": 100
}
```

### POST /v1/responses dengan Tool Calling

**Step 1** — Kirim request dengan tools:
```json
{
  "model": "gpt-5.4",
  "input": [{"type": "message", "role": "user", "content": "List files"}],
  "tools": [{"type": "function", "name": "shell", "description": "Run cmd", "parameters": {...}}],
  "max_output_tokens": 200
}
```

**Response (Step 1):**
```json
{
  "id": "resp_abc",
  "output": [
    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "I'll list the files."}]},
    {"type": "function_call", "call_id": "call_123", "name": "shell", "arguments": "{\"command\":\"ls -la\"}"}
  ]
}
```

**Step 2** — Kirim tool output dengan chaining:
```json
{
  "model": "gpt-5.4",
  "input": [
    {"type": "function_call_output", "call_id": "call_123", "output": "file1.txt\nfile2.py"}
  ],
  "previous_response_id": "resp_abc",
  "tools": [...],
  "max_output_tokens": 200
}
```

### GET /v1/responses/{response_id}

Ambil response sebelumnya (untuk chaining):

```bash
curl -H "Authorization: Bearer YOUR_KEY" \
  https://gateway.jembatanai.com/v1/responses/resp_abc123
```

### DELETE /v1/responses/{response_id}

Hapus response:

```bash
curl -X DELETE -H "Authorization: Bearer YOUR_KEY" \
  https://gateway.jembatanai.com/v1/responses/resp_abc123
```

### GET /v1/models

Daftar model tersedia:

```bash
curl -H "Authorization: Bearer YOUR_KEY" \
  https://gateway.jembatanai.com/v1/models
```

---

## Troubleshooting

### Error: "Incorrect API key ... url: https://api.openai.com/"

**Penyebab:** `auth.json` (dari login ChatGPT) meng-override config.

**Fix:**
```powershell
# Windows PowerShell
codex logout
Remove-Item "$env:USERPROFILE\.codex\auth.json*" -Force
codex
```

```bash
# Linux/macOS
codex logout
rm -f ~/.codex/auth.json
codex
```

### Error: "Missing API key"

**Penyebab:** Environment variable `JEMBATANAI_API_KEY` tidak ter-set.

**Fix:**
```powershell
# Windows — set ulang
$env:JEMBATANAI_API_KEY = "gw-your-key-here"
[Environment]::SetEnvironmentVariable("JEMBATANAI_API_KEY", "gw-your-key-here", "User")
```

```bash
# Linux — set ulang
export JEMBATANAI_API_KEY="gw-your-key-here"
echo 'export JEMBATANAI_API_KEY="gw-your-key-here"' >> ~/.bashrc
```

### Error: "Daily quota exceeded"

Anda sudah melewati batas harian. Tunggu sampai midnight UTC atau hubungi admin untuk reset.

### Error: "Subscription expired"

API key sudah expired. Hubungi admin untuk perpanjang.

### Codex minta login / pilih provider

Jangan pilih "Sign in with ChatGPT". Jika muncul prompt login, berarti `auth.json` masih ada:

```powershell
codex logout
# Tutup Codex, buka lagi
codex
```

### Model tidak muncul di /model

Pastikan `config.toml` sudah benar dan `model_provider = "jembatanai"`.

### Streaming disconnect / timeout

Coba ganti model ke yang lebih cepat:
```
/model → gpt-4o-mini
```

### Cek apakah koneksi berhasil

```powershell
# Test dari PowerShell
curl https://gateway.jembatanai.com/health
```

```bash
# Test dari terminal
curl -H "Authorization: Bearer YOUR_KEY" https://gateway.jembatanai.com/v1/models
```

---

## FAQ

**Q: Apakah gratis?**  
A: Ya, model-model yang tersedia adalah model gratis dari Kilo/OpenRouter.

**Q: Apakah data saya aman?**  
A: Request diproses melalui gateway JembatanAI. Tidak ada data yang disimpan selain log usage untuk quota.

**Q: Bisakah pakai model OpenAI/Claude?**  
A: Tidak. Ini menggunakan model gratis dari Kilo. Untuk OpenAI/Claude asli, gunakan API key mereka langsung.

**Q: Berapa batas harian?**  
A: Tergantung plan. Team plan: 5000 request/hari.

**Q: Bisakah pakai di VS Code / Cursor?**  
A: Codex CLI hanya untuk terminal. Untuk IDE, gunakan extension yang mendukung OpenAI API.

**Q: Bagaimana cara ganti API key?**  
```powershell
# Windows
$env:JEMBATANAI_API_KEY = "gw-new-key-here"
[Environment]::SetEnvironmentVariable("JEMBATANAI_API_KEY", "gw-new-key-here", "User")
```

```bash
# Linux
export JEMBATANAI_API_KEY="gw-new-key-here"
```

**Q: Bagaimana cara update Codex CLI?**
```bash
npm update -g @openai/codex
```

**Q: Config file ada di mana?**

| OS | Lokasi |
|----|--------|
| Windows | `%USERPROFILE%\.codex\config.toml` |
| Linux | `~/.codex/config.toml` |
| macOS | `~/.codex/config.toml` |

**Q: Log ada di mana?**
```bash
# Lihat log service (server-side)
journalctl -u jembatanai-codex -f

# Lihat log Codex CLI (client-side)
ls ~/.codex/logs_*.sqlite
```

---

## Konfigurasi Lanjutan

### Custom Sandbox Mode

```toml
# config.toml

# Mode aman (default OpenAI) — minta izin sebelum run
sandbox_mode = "read-only"
approval_policy = "on-failure"

# Mode penuh — langsung jalan (hati-hati!)
sandbox_mode = "danger-full-access"
approval_policy = "never"
```

### Custom Reasoning Effort

```toml
model_reasoning_effort = "low"     # Cepat, kurang mendalam
model_reasoning_effort = "medium"  # Seimbang
model_reasoning_effort = "high"    # Lambat, lebih mendalam (default)
```

### Non-Interactive Mode

```bash
# Jalankan satu perintah tanpa interaksi
codex exec "buatkan file hello.py yang print hello world"

# Skip git check
codex exec --skip-git-repo-check "explain what ls does"
```

---

## Info Teknis (Untuk Developer)

### Endpoint yang Tersedia

| Endpoint | Method | Auth | Fungsi |
|----------|--------|------|--------|
| `/v1/responses` | POST | Bearer | Responses API (primary) |
| `/v1/responses/{id}` | GET | Bearer | Ambil response |
| `/v1/responses/{id}` | PUT | Bearer | Update response |
| `/v1/responses/{id}` | DELETE | Bearer | Hapus response |
| `/v1/chat/completions` | POST | Bearer | Chat API |
| `/v1/models` | GET | Bearer | Model list |
| `/health` | GET | Public | Health check |

### Plan & Quota

| Plan | Harian | Models | Harga |
|------|--------|--------|-------|
| free | 20 | 1 | Gratis |
| starter | 200 | 3 | - |
| pro | 1000 | 10 | - |
| team | 5000 | all | - |

### Wire Protocol

```
wire_api: "responses"   ← Codex CLI menggunakan ini
wire_api: "chat"        ← Standard OpenAI Chat
```

---

**Dokumentasi ini berdasarkan hasil testing langsung pada 2026-03-26.**  
**Semua endpoint sudah diverifikasi working melalui `gateway.jembatanai.com`.**
