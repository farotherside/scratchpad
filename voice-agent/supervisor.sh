#!/bin/bash
# Supervisor: keeps ngrok + voice server running, restarts on crash

OPENAI_API_KEY="${OPENAI_API_KEY}"
PORT=3000
LOG=/tmp/voice.log
NGROK_LOG=/tmp/ngrok.log

# Kill anything existing
kill $(lsof -ti:$PORT) 2>/dev/null || true
pkill -f ngrok 2>/dev/null || true
sleep 2

# Start ngrok
ngrok http $PORT --log=stdout > $NGROK_LOG 2>&1 &
NGROK_PID=$!
echo "[supervisor] ngrok PID: $NGROK_PID"
sleep 5

# Get URL
PUBLIC_URL=$(curl -s http://localhost:4040/api/tunnels 2>/dev/null | python3 -c "
import sys,json
try:
    t=json.load(sys.stdin)['tunnels']
    print([x['public_url'] for x in t if x['proto']=='https'][0])
except: pass
")

if [ -z "$PUBLIC_URL" ]; then
    echo "[supervisor] ERROR: no ngrok URL"
    exit 1
fi
echo "[supervisor] URL: $PUBLIC_URL"

# Update Twilio webhook
curl -s -X POST "https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}/IncomingPhoneNumbers/${TWILIO_PHONE_SID}.json" \
  -u "${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}" \
  --data-urlencode "VoiceUrl=$PUBLIC_URL/voice" > /dev/null
echo "[supervisor] Twilio webhook updated"

# Server restart loop
while true; do
    echo "[supervisor] Starting server..." >> $LOG
    PUBLIC_URL="$PUBLIC_URL" OPENAI_API_KEY="$OPENAI_API_KEY" \
        node /home/drew/.openclaw/workspace/voice-agent/server.js >> $LOG 2>&1
    echo "[supervisor] Server exited, restarting in 2s..." >> $LOG
    sleep 2
done
