# Can You Repeat That?

A voice-controlled SO-101 arm for loud rooms. It acts on clear commands, asks
"Can you repeat that?" when the audio or the words are not trustworthy, ignores
talk that is not meant for it, and always stops on "stop".

A robot that knows who is speaking to it and acts on what they say.

```
mic ─► Quail Voice Focus 2.3 ─► Deepgram nova-3 (streaming STT) ─► gate ─► SO-101 arm
 │              │                                                    │
 └ Tyto 1.2     └ Tyto 1.2 (cleaned): how trustworthy is the audio?  ├─► Jev: act / ask / ignore
                                                                     └─► Deepgram Aura voice out of
                                                                         Reachy Mini + head/antenna reactions
```

- Clear command: the arm moves, Reachy says "On it!" and nods.
- Tyto risk too high: the arm stays still and Reachy explains ("Too many voices... Can you repeat that?").
- Talk not meant for the robot: Jev ignores it.
- "Stop" skips every gate and halts the arm mid-motion.

## Demo run (two terminals)

```
./run.sh arm      # SO-101 motion server on :8092
./run.sh bridge   # audio, Deepgram voice loop, gate, screen on http://127.0.0.1:8787
```

Needs `AIC_SDK_LICENSE`, `DEEPGRAM_API_KEY`, `TYPESAFE_API_KEY` in `.env`, and the Reachy Mini
daemon on :8000 for head moves. Speaker: `SPEAKER_DEVICE` (default "Reachy Mini Audio").

The screen has switches for Voice Focus, the Tyto gate and the local voice loop, plus a live
Tyto limit slider.

## Vapi path (MCP tool, optional)

The bridge also serves the arm as an MCP server for a Vapi assistant. Turn "Local voice" off on
the screen first so only one agent acts.

```
MacBook mic ─► Quail Voice Focus 2.3 ─► BlackHole 2ch ─► Vapi web call (Deepgram STT, LLM)
                                                                 │ MCP tool call
                                         gate (Tyto + Jev) ◄─────┘ ─► arm_server.py ─► SO-101
```

Gotcha: a Vapi `PATCH /tool` with only `messages` wipes the MCP `server` URL; always send `server`.

### Run (four terminals)

| Terminal | Command | What it is |
| --- | --- | --- |
| 1 | `./run.sh arm` (or `./run.sh arm-fake` with no hardware) | SO-101 motion server on :8092 |
| 2 | `./run.sh bridge` | MCP server, audio, gate, screen on http://127.0.0.1:8787 |
| 3 | `./run.sh tunnel` | Public URL for Vapi (cloudflared quick tunnel) |
| 4 | `./run.sh vapi` | Creates or updates the Vapi MCP tool and assistant |

Before step 4 put the Vapi private key in `.env` as `VAPI_API_KEY=`. Rerun step 4
whenever the tunnel restarts (the URL changes).

Then open the printed dashboard link, click Talk, and select **BlackHole 2ch** as
the microphone in Chrome (address bar mic icon, or chrome://settings/content/microphone).
Use headphones for the agent voice so it does not feed back into the mic.

## Motions

Motions are JSON files in `motions/`. `wave`, `rest`, `point_person` are copied
from the booth. Record new ones by moving the arm by hand:

```
curl -X POST 'localhost:8092/record/hand_over?seconds=6'   # while ./run.sh arm is running, 3 s countdown
./run.sh record hand_over 6                                # or standalone, with the server stopped
./run.sh play hand_over
```

Restart the bridge after adding motions so Vapi sees the new names.

## Tuning in the room

- Screen: http://127.0.0.1:8787. The slider sets the Tyto risk limit live.
- Click "Raw mic" or "After Voice Focus 2.3" to choose which Tyto reading gates the arm.
- Defaults live in `.env`: `TYTO_RISK_LIMIT`, `TYTO_GATE` (`raw` or `vf`), `MIC_DEVICE`, `VF_OUTPUT_DEVICE`.

## Test without Vapi

```
curl -X POST -G localhost:8787/test/move --data-urlencode action=wave --data-urlencode 'heard=wave at me'
curl -X POST localhost:8787/test/stop
```

## Files

- `bridge.py`: MCP tools (`move_arm`, `stop_arm`), Voice Focus to BlackHole, dual Tyto, Jev, gate, `/state`.
- `arm_server.py`: record, replay and a stop that halts mid-motion. Runs on `~/code/reachy-booth/.booth-venv` (lerobot, calibration `workshop_so101`).
- `vapi/setup.py`, `vapi/assistant_prompt.md`: Vapi assistant and MCP tool.
- `web/index.html`: demo screen.

## Not verified yet

- Real arm: only the fake arm was run. The replay helpers are the booth's tested ones; `stop` and `/record` are new.
- Live Vapi call through the tunnel.
- Echo: agent voice on speakers will reach the mic. Use headphones.
- An old booth gesture server (started Nov 5) still listens on :8091. This project uses :8092. Kill the old one before plugging the arm in so it cannot grab the serial port.
