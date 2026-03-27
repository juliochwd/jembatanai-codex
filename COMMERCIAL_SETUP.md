# JembatanAI-Codex Commercial Setup

## Overview

JembatanAI-Codex adalah proxy komersial untuk OpenAI Codex CLI yang menggunakan **JembatanAI Commercial Gateway** (bukan direct Kilo API).

## Architecture

```
Codex CLI
    ↓
JembatanAI-Codex (Port 4110)
    ↓
JembatanAI Gateway (Port 4100)
    ↓
Kilo API (with TOR rotation)
```

## Benefits

1. **Commercial Control**: API key management melalui JembatanAI
2. **Monitoring**: Semua request tercatat di gateway
3. **TOR Rotation**: Automatic IP rotation via gateway
4. **Billing**: Usage tracking untuk customer billing
5. **Rate Limiting**: Per-customer rate limits

## Setup

### 1. Get JembatanAI API Key

```bash
# Get your commercial API key from JembatanAI dashboard
# or generate via admin endpoint
```

### 2. Configure .env

```bash
# Edit /home/ubuntu/jembatanai-codex/.env
JEMBATANAI_API_KEY=gw-your-commercial-key-here
```

### 3. Restart Service

```bash
sudo systemctl restart jembatanai-codex
```

### 4. Test

```bash
# Health check
curl -sk http://localhost:4110/health

# Test with Codex
codex
/model → kilo-mimo-v2-pro
```

## API Key Management

### Generate API Key

```bash
# Via JembatanAI admin endpoint
curl -X POST http://localhost:4100/api/keys \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -d '{"name": "codex-commercial", "plan": "pro"}'
```

### Rotate API Key

```bash
# Update .env
JEMBATANAI_API_KEY=gw-new-key-here

# Restart
sudo systemctl restart jembatanai-codex
```

## Monitoring

### Check Usage

```bash
# Via JembatanAI dashboard
# or via API
curl -sk http://localhost:4100/api/usage \
  -H "Authorization: Bearer $API_KEY"
```

### Check Logs

```bash
# JembatanAI-Codex logs
journalctl -u jembatanai-codex -f

# JembatanAI Gateway logs
journalctl -u jembatanai-proxy -f
```

## Troubleshooting

### 401 Unauthorized

```bash
# Check API key in .env
cat /home/ubuntu/jembatanai-codex/.env | grep JEMBATANAI_API_KEY

# Test gateway directly
curl -sk http://localhost:4100/health
```

### Service Won't Start

```bash
# Check logs
journalctl -u jembatanai-codex -n 50

# Test manually
cd /home/ubuntu/jembatanai-codex
python3 -m uvicorn proxy_codex:app --port 4110
```

## Commercial Features

- ✅ API key management
- ✅ Usage tracking
- ✅ Rate limiting per customer
- ✅ TOR IP rotation
- ✅ Monitoring & alerting
- ✅ Billing integration ready

## Support

For commercial support, contact:
- Email: support@jembatanai.com
- Telegram: @jembatanai

---

**Last Updated**: 2026-03-24  
**Status**: ✅ Production Ready
