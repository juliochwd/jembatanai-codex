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
Write-Host "   Codex CLI — JembatanAI Gateway                      " -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

# ─── 1. Cek Codex CLI ────────────────────────────────────────
Write-Host "[1/7] Mengecek Codex CLI..." -ForegroundColor Yellow
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
Write-Host "[2/7] Logout dari OpenAI (menghapus auth.json)..." -ForegroundColor Yellow

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

# Hapus juga config.toml lama jika ada
$oldConfig = Join-Path $codexDir "config.toml"
if (Test-Path $oldConfig) {
    Remove-Item -Path $oldConfig -Force -ErrorAction SilentlyContinue
    Write-Host "  DELETED: old config.toml" -ForegroundColor DarkYellow
}

Write-Host "  OK: Semua auth OpenAI dibersihkan" -ForegroundColor Green

# ─── 3. Minta API Key dari Customer ──────────────────────────────
Write-Host "[3/7] Masukkan API Key Anda..." -ForegroundColor Yellow
Write-Host ""
Write-Host "  Dapatkan API Key Anda di:" -ForegroundColor White
Write-Host "  https://gateway.jembatanai.com/dashboard.html" -ForegroundColor Cyan
Write-Host "  (Menu 'API Keys' di dashboard)" -ForegroundColor Gray
Write-Host ""
Write-Host "  Format API Key: gw-xxxxxxxxxxxxxxxxxxxx-xxxxxxxxxxxxxxxxxxxx" -ForegroundColor Gray
Write-Host ""

do {
    Write-Host "  Masukkan API Key Anda: " -NoNewline -ForegroundColor White
    $apiKey = Read-Host
    $apiKey = $apiKey.Trim()

    if ([string]::IsNullOrEmpty($apiKey)) {
        Write-Host "  ERROR: API Key tidak boleh kosong!" -ForegroundColor Red
        continue
    }

    # Validasi format: gw-xxxxxxxxxxxxxxxxxxxx-xxxxxxxxxxxxxxxxxxxx
    if ($apiKey -notmatch '^gw-[a-zA-Z0-9]{20}-[a-zA-Z0-9]{20}$') {
        Write-Host "  ERROR: Format API Key tidak valid!" -ForegroundColor Red
        Write-Host "  Format yang benar: gw-xxxxxxxxxxxxxxxxxxxx-xxxxxxxxxxxxxxxxxxxx" -ForegroundColor Gray
        continue
    }

    break
} while ($true)

Write-Host "  OK: API Key diterima" -ForegroundColor Green

# ─── 4. Set API Key (FORCE - Semua Session) ─────────────────────
Write-Host "[4/7] Set API Key..." -ForegroundColor Yellow

# Clear OLD API key from environment first
$env:OPENAI_API_KEY = $null
$env:JEMBATANAI_API_KEY = $null

# Set di current session (Codex expects OPENAI_API_KEY)
$env:OPENAI_API_KEY = $apiKey
$env:JEMBATANAI_API_KEY = $apiKey

# Set di User level (persist untuk future sessions)
[Environment]::SetEnvironmentVariable("OPENAI_API_KEY", $apiKey, "User")
[Environment]::SetEnvironmentVariable("JEMBATANAI_API_KEY", $apiKey, "User")

# Set di Machine level (fallback, requires admin)
try {
    [Environment]::SetEnvironmentVariable("OPENAI_API_KEY", $apiKey, "Machine")
    [Environment]::SetEnvironmentVariable("JEMBATANAI_API_KEY", $apiKey, "Machine")
} catch {
    # Machine level might fail without admin rights, skip silently
}

Write-Host "  OK: API Key diset" -ForegroundColor Green

# ─── 5. Tulis config.toml ────────────────────────────────────
Write-Host "[5/7] Menulis config.toml..." -ForegroundColor Yellow

# Ensure directory exists
if (-not (Test-Path $codexDir)) {
    New-Item -ItemType Directory -Path $codexDir -Force | Out-Null
}

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
env_key = "OPENAI_API_KEY"
wire_api = "responses"

[notice]
hide_full_access_warning = true
hide_rate_limit_model_nudge = true
"@ | Set-Content -Path $configFile -Encoding UTF8

Write-Host "  OK: $configFile" -ForegroundColor Green
Write-Host "  Model: gpt-5.4" -ForegroundColor Gray

# ─── 6. Verifikasi koneksi ke Gateway ────────────────────────────────
Write-Host "[6/7] Verifikasi koneksi ke gateway..." -ForegroundColor Yellow

$baseUrl = "https://gateway.jembatanai.com"
$testResult = curl.exe -s -o NUL -w "%{http_code}" "$baseUrl/health" 2>$null
if ($testResult -eq "200" -or $testResult -eq "404") {
    Write-Host "  OK: Gateway reachable ($testResult)" -ForegroundColor Green
} else {
    Write-Host "  WARNING: Gateway returned $testResult" -ForegroundColor Yellow
}

# Test API Key dengan request nyata
Write-Host "  Testing API Key..." -ForegroundColor Gray
$testResponse = curl.exe -s -X POST "https://gateway.jembatanai.com/v1/responses" `
    -H "Authorization: Bearer $apiKey" `
    -H "Content-Type: application/json" `
    -d '{"model":"gpt-5.4","input":"test"}' `
    --max-time 30 2>$null

if ($LASTEXITCODE -eq 0 -and $testResponse -match '"status":"completed"') {
    Write-Host "  OK: API Key VALID! Gateway responding." -ForegroundColor Green
} elseif ($LASTEXITCODE -eq 0 -and $testResponse -match '"error"') {
    $errorMsg = ($testResponse | ConvertFrom-Json).error.message
    Write-Host "  ERROR: API Key ditolak - $errorMsg" -ForegroundColor Red
    exit 1
} else {
    Write-Host "  WARNING: Could not verify automatically" -ForegroundColor Yellow
}

# ─── 7. Setup Complete ────────────────────────────────────────────────
Write-Host "[7/7] Setup Complete!" -ForegroundColor Green

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
Write-Host "  Model: gpt-5.4" -ForegroundColor Gray
Write-Host "  Ganti model: ketik /model di dalam Codex" -ForegroundColor Gray
Write-Host ""
Write-Host "  PENTING:" -ForegroundColor Red
Write-Host "  - JANGAN pilih 'Sign in with ChatGPT'" -ForegroundColor Red
Write-Host "  - Jika diminta login, CLOSE dan cek config" -ForegroundColor Red
Write-Host ""
Write-Host "  Jika ada error 401 Unauthorized:" -ForegroundColor Yellow
Write-Host "  1. Buka PowerShell baru" -ForegroundColor Yellow
Write-Host "  2. Ketik: \$env:OPENAI_API_KEY" -ForegroundColor Yellow
Write-Host "  3. Jika kosong, set manual:" -ForegroundColor Yellow
Write-Host '     $env:OPENAI_API_KEY = "' + $apiKey + '"' -ForegroundColor Yellow
Write-Host "     codex" -ForegroundColor Yellow
Write-Host ""
