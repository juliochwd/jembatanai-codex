# ═══════════════════════════════════════════════════════════════════════
#  JembatanAI-Codex — One-Click Setup (Windows PowerShell)
#  
#  Cara pakai (copy-paste di PowerShell):
#
#  irm https://gateway.jembatanai.com/setup-codex.ps1 | iex
#
# ═══════════════════════════════════════════════════════════════════════

Write-Host ""
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "   JembatanAI-Codex — One-Click Setup                  " -ForegroundColor Cyan
Write-Host "   Codex CLI + Kilo Free Models                        " -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

# ─── 1. Cek Codex CLI ────────────────────────────────────────
Write-Host "[1/5] Mengecek Codex CLI..." -ForegroundColor Yellow
$codexPath = Get-Command codex -ErrorAction SilentlyContinue
if (-not $codexPath) {
    Write-Host "  Installing Codex CLI..." -ForegroundColor Yellow
    $npmPath = Get-Command npm -ErrorAction SilentlyContinue
    if (-not $npmPath) {
        Write-Host "  ERROR: npm tidak ditemukan. Install Node.js: https://nodejs.org/" -ForegroundColor Red
        exit 1
    }
    npm install -g @openai/codex
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ERROR: Gagal install Codex CLI" -ForegroundColor Red
        exit 1
    }
}
$ver = codex --version 2>$null
Write-Host "  OK: Codex CLI $ver" -ForegroundColor Green

# ─── 2. Logout dari ChatGPT/OpenAI (PENTING!) ───────────────
Write-Host "[2/5] Logout dari OpenAI (menghapus auth.json)..." -ForegroundColor Yellow

# Method 1: codex logout (proper way)
codex logout 2>$null | Out-Null

# Method 2: force delete jika masih ada
$codexDir = "$env:USERPROFILE\.codex"
@("auth.json", "auth.json.disabled", "auth.json.bak", "auth.json.old") | ForEach-Object {
    $f = Join-Path $codexDir $_
    if (Test-Path $f) {
        Remove-Item -Path $f -Force -ErrorAction SilentlyContinue
        Write-Host "  DELETED: $_" -ForegroundColor DarkYellow
    }
}
Write-Host "  OK: Semua auth OpenAI dibersihkan" -ForegroundColor Green

# ─── 3. Setup API Key ────────────────────────────────────────
Write-Host "[3/5] API Key..." -ForegroundColor Yellow

# Pakai default admin key (customer bisa ganti nanti)
$apiKey = "gw-admin-SuG66BxPfKh3JzQUC9Rb-9zn9SvrQFYo5YBFhU6WC"

# Set env var permanen + session
$env:JEMBATANAI_API_KEY = $apiKey
[Environment]::SetEnvironmentVariable("JEMBATANAI_API_KEY", $apiKey, "User")
Write-Host "  OK: JEMBATANAI_API_KEY diset" -ForegroundColor Green

# ─── 4. Tulis config.toml ────────────────────────────────────
Write-Host "[4/5] Menulis config.toml..." -ForegroundColor Yellow

$configFile = "$codexDir\config.toml"
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
"@ | Set-Content -Path $configFile -Encoding UTF8

Write-Host "  OK: $configFile" -ForegroundColor Green

# ─── 5. Verifikasi koneksi ───────────────────────────────────
Write-Host "[5/5] Verifikasi koneksi ke gateway..." -ForegroundColor Yellow

$testResult = curl.exe -s -o NUL -w "%{http_code}" "https://gateway.jembatanai.com/health" 2>$null
if ($testResult -eq "200" -or $testResult -eq "404") {
    Write-Host "  OK: Gateway reachable" -ForegroundColor Green
} else {
    Write-Host "  WARNING: Gateway returned $testResult (might still work)" -ForegroundColor Yellow
}

# ─── Selesai ─────────────────────────────────────────────────
Write-Host ""
Write-Host "========================================================" -ForegroundColor Green
Write-Host "   SETUP SELESAI!" -ForegroundColor Green
Write-Host "========================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  CARA PAKAI:" -ForegroundColor White
Write-Host ""
Write-Host "  1. Tutup PowerShell ini" -ForegroundColor White
Write-Host "  2. Buka PowerShell BARU" -ForegroundColor White
Write-Host "  3. Ketik:" -ForegroundColor White
Write-Host ""
Write-Host "     codex" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Model: gpt-5.4 (MiMo V2 Pro - free, 1M ctx)" -ForegroundColor Gray
Write-Host "  Ganti model: ketik /model di dalam Codex" -ForegroundColor Gray
Write-Host ""
Write-Host "  PENTING:" -ForegroundColor Red
Write-Host "  - JANGAN pilih 'Sign in with ChatGPT'" -ForegroundColor Red
Write-Host "  - Jika diminta login, CLOSE dan cek config" -ForegroundColor Red
Write-Host ""
