# voice-agent

A real-time AI phone call agent. Twilio streams audio into the server, Deepgram transcribes speech to text, Claude generates a conversational reply, and ElevenLabs synthesises the response back to audio — all in a live phone call.

## Architecture

```
Twilio (call) → WebSocket media stream
    → Deepgram STT (nova-3, streaming)
    → Claude (claude-haiku, conversational)
    → ElevenLabs TTS (streaming MP3 → ffmpeg → μ-law)
    → Twilio (audio back to caller)
```

At call end, a full transcript is sent to Signal via OpenClaw.

## Stack

| Component | Service |
|-----------|---------|
| Telephony | Twilio |
| STT | Deepgram nova-3 |
| LLM | Anthropic Claude Haiku |
| TTS | ElevenLabs Flash v2.5 |
| Tunnel | ngrok |
| Runtime | Node.js / Express |

## Setup

```bash
npm install
```

Requires `ngrok` to be installed and authenticated.

## Running

```bash
# One-shot start (handles ngrok + Twilio webhook update)
bash start.sh

# Supervised (auto-restart on crash)
bash supervisor.sh

# At boot (via cron @reboot)
bash autostart.sh
```

## Outbound calls

The server exposes a `/call` endpoint to trigger an outbound call:

```bash
curl -X POST http://localhost:3000/call \
  -d "to=+1XXXXXXXXXX" \
  -d "greeting=Hey, this is Serif calling."
```

Twilio also routes inbound calls to `/voice` automatically once the webhook is set.

## Files

```
server.js       ← Main server: Express + WebSocket + full call pipeline
start.sh        ← Start script: kills old procs, starts ngrok + server, sets Twilio webhook
supervisor.sh   ← Supervisor loop: restarts server on crash
autostart.sh    ← @reboot cron entry for persistent background operation
package.json    ← Node dependencies
```

## How it works

1. **Call connects** → Twilio posts to `/voice`, which returns TwiML to open a media stream WebSocket
2. **Greeting** → Server immediately speaks a greeting via ElevenLabs TTS
3. **Speech loop** → Deepgram streams transcription; on utterance end, text is sent to Claude
4. **Reply** → Claude's response is streamed through ElevenLabs → ffmpeg (MP3→μ-law) → Twilio
5. **Transcript** → On call end, full conversation is sent to Signal
