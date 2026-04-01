#!/bin/bash
set -e

PORT=3000
TWILIO_SID="${TWILIO_ACCOUNT_SID}"
TWILIO_AUTH="${TWILIO_AUTH_TOKEN}"
TWILIO_PHONE_SID="${TWILIO_PHONE_SID}"
OPENAI_KEY=$(cat ~/.openclaw/openclaw.json | python3 -c "import sys,json; c=json.load(sys.stdin); print(c['skills']['entries']['openai-image-gen']['apiKey'])" 2>/dev/null || echo "${OPENAI_API_KEY}")

echo "[start] Killing old processes..."
pkill -f "voice-agent/server.js" 2>/dev/null || true
pkill -f "ngrok http $PORT" 2>/dev/null || true
sleep 1

echo "[start] Starting voice agent server..."
OPENAI_API_KEY="$OPENAI_KEY" node server.js &
SERVER_PID=$!
echo "[start] Server PID: $SERVER_PID"
sleep 2

echo "[start] Starting ngrok..."
ngrok http $PORT --log=stdout --log-format=json > /tmp/ngrok-voice.log 2>&1 &
NGROK_PID=$!
echo "[start] ngrok PID: $NGROK_PID"
sleep 3

# Get public URL from ngrok API
PUBLIC_URL=$(curl -s http://localhost:4040/api/tunnels | python3 -c "
import sys, json
tunnels = json.load(sys.stdin)['tunnels']
for t in tunnels:
    if t['proto'] == 'https':
        print(t['public_url'])
        break
")

if [ -z "$PUBLIC_URL" ]; then
  echo "[error] Could not get ngrok URL"
  kill $SERVER_PID $NGROK_PID 2>/dev/null
  exit 1
fi

echo "[start] Public URL: $PUBLIC_URL"

# Restart server with PUBLIC_URL set
kill $SERVER_PID 2>/dev/null
sleep 1
PUBLIC_URL="$PUBLIC_URL" OPENAI_API_KEY="$OPENAI_KEY" node server.js &
SERVER_PID=$!
echo "[start] Server restarted with PID: $SERVER_PID"
sleep 2

# Update Twilio webhook (for incoming calls too)
echo "[start] Updating Twilio voice webhook..."
curl -s -X POST "https://api.twilio.com/2010-04-01/Accounts/$TWILIO_SID/IncomingPhoneNumbers/$TWILIO_PHONE_SID.json" \
  -u "$TWILIO_SID:$TWILIO_AUTH" \
  --data-urlencode "VoiceUrl=$PUBLIC_URL/voice" \
  --data-urlencode "VoiceMethod=POST" | python3 -c "import sys,json; r=json.load(sys.stdin); print('[twilio] Webhook set to:', r.get('voice_url','?'))"

echo ""
echo "════════════════════════════════════════"
echo "  Serif Voice Agent is LIVE"
echo "  Public URL: $PUBLIC_URL"
echo "  Server PID: $SERVER_PID"
echo "  ngrok PID:  $NGROK_PID"
echo ""
echo "  To call Drew now:"
echo "  curl -X POST $PUBLIC_URL/call"
echo ""
echo "  Or just call $TWILIO_PHONE_SID from your phone"
echo "════════════════════════════════════════"

# Keep script alive
wait $SERVER_PID
