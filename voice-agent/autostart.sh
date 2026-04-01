#!/bin/bash
# Auto-start script for serif-voice — run via cron @reboot
# Add to crontab: @reboot /home/drew/.openclaw/workspace/voice-agent/autostart.sh

sleep 10  # wait for network

# Kill any stale processes
pkill -f ngrok 2>/dev/null
lsof -ti:3000 | xargs kill -9 2>/dev/null
sleep 2

# Start ngrok
nohup ngrok http 3000 &>/tmp/ngrok.log &
sleep 6

# Verify tunnel is up
PUBLIC_URL=$(curl -s http://localhost:4040/api/tunnels | python3 -c "
import sys,json
try:
    t=json.load(sys.stdin)['tunnels']
    print([x['public_url'] for x in t if x['proto']=='https'][0])
except: pass
" 2>/dev/null)

if [ -z "$PUBLIC_URL" ]; then
    echo "[autostart] ERROR: ngrok tunnel not available" >> /tmp/voice.log
    exit 1
fi

# Update Twilio webhook in case URL changed
curl -s -X POST "https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}/IncomingPhoneNumbers/${TWILIO_PHONE_SID}.json" \
  -u "${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}" \
  --data-urlencode "VoiceUrl=$PUBLIC_URL/voice" > /dev/null

# Start server
cd /home/drew/.openclaw/workspace/voice-agent
exec env PUBLIC_URL="$PUBLIC_URL" node server.js >> /tmp/voice.log 2>&1
