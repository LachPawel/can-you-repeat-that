"""Can You Repeat That?  Vapi <-> SO-101 bridge.

One process, three jobs:

1. MCP server (Streamable HTTP at /mcp) that Vapi connects to as an MCP tool.
   Tools: move_arm(action, heard) and stop_arm().
2. Audio (ai-coustics SDK): mic -> Quail Voice Focus 2.3 -> BlackHole 2ch, which is
   the microphone the Vapi call uses. Tyto 1.2 scores both the raw mic and the cleaned
   stream, so every command is judged against how trustworthy the audio was.
3. Jev (TypeSafe System One) decides act / ask / ignore for commands that pass
   the Tyto gate. Stop never goes through the gate.

    ./run.sh bridge        http://127.0.0.1:8787  (status screen at /)

Local test without Vapi:
    curl -X POST 'localhost:8787/test/move?action=wave&heard=wave+at+me'
    curl -X POST 'localhost:8787/test/stop'
"""
from __future__ import annotations

import collections
import os
import threading
import time
from pathlib import Path

import httpx
import numpy as np
import uvicorn
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

ARM_URL = os.getenv("ARM_URL", "http://127.0.0.1:8092")
PORT = int(os.getenv("BRIDGE_PORT", "8787"))
MOTIONS = ROOT / "motions"
TYTO_MODELS = os.getenv("TYTO_MODELS_DIR", str(Path.home() / "code/tyto-real-time-demo/models"))
REPEAT = "Can you repeat that?"
AGENT_PHRASES = {"on it", "stopped", "can you repeat that", "standing by", "arm is ready",
                 "the arm is not responding"}
JEV_MIN_CONFIDENCE = 0.40
SCORE_STALE_SECONDS = 4.0  # an older Tyto score no longer describes the command


def motions() -> list[str]:
    return sorted(p.stem for p in MOTIONS.glob("*.json"))


# --------------------------------------------------------------------------- #
# Shared state (also what the status screen shows)                            #
# --------------------------------------------------------------------------- #

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.limit = float(os.getenv("TYTO_RISK_LIMIT", "0.45"))
        self.tyto_state = "off"
        self.tyto_text = "Tyto not started"
        self.scores: dict = {"raw": None, "vf": None}  # Tyto on the raw mic / after Quail Voice Focus
        self.scores_at = {"raw": 0.0, "vf": 0.0}
        self.gate = os.getenv("TYTO_GATE", "vf")  # which reading gates the arm
        self.vf_text = "Voice Focus not started"
        self.partial = ""         # what Deepgram is hearing right now
        self.voice_text = "voice loop off"
        self.local_voice = True  # off: only the Vapi agent listens (no double commands)
        self.vf_on = True    # off: the raw mic goes to Vapi untouched
        self.gate_on = True  # off: a high Tyto risk no longer blocks the arm
        self.level = 0.0  # mic RMS, for the meter
        self.events = collections.deque(maxlen=40)
        self.arm: dict = {}

    def fresh(self, source: str) -> dict | None:
        scores = self.scores.get(source)
        if scores is None or time.monotonic() - self.scores_at[source] > SCORE_STALE_SECONDS:
            return None
        return scores

    def gate_scores(self) -> dict | None:
        """Fresh scores of the gating source (falls back to the raw mic), or None."""
        with self.lock:
            return self.fresh(self.gate) or self.fresh("raw")

    def event(self, **fields) -> dict:
        record = {"t": time.time(), **fields}
        with self.lock:
            self.events.appendleft(record)
        print(f"[{record.get('decision', '?'):>6}] {record.get('action', '')!s:<14} "
              f"risk={record.get('risk')} src={record.get('source')} heard={record.get('heard', '')!r} "
              f"-> {record.get('say', '')}", flush=True)
        return record

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "limit": self.limit,
                "tyto": {"state": self.tyto_state, "text": self.tyto_text, "gate": self.gate,
                         "raw": self.fresh("raw"), "vf": self.fresh("vf")},
                "vf": self.vf_text, "vf_on": self.vf_on, "gate_on": self.gate_on,
                "partial": self.partial, "voice": self.voice_text,
                "speaking": bool(VOICE and VOICE.speaking), "local_voice": self.local_voice,
                "level": self.level,
                "events": list(self.events),
                "arm": self.arm,
                "motions": motions(),
                "jev": bool(os.getenv("TYPESAFE_API_KEY")),
            }


STATE = State()


# --------------------------------------------------------------------------- #
# Audio: mic -> Quail Voice Focus 2.3 -> BlackHole (the mic Vapi hears),      #
#        with Tyto 1.2 scoring both the raw mic and the cleaned stream         #
# --------------------------------------------------------------------------- #

VF_MODEL = "quail-vf-2.3-l-16khz"
TYTO_MODEL = "tyto-1.2-l-16khz"
RATE = 16000
OUT_GAIN = float(os.getenv("OUT_GAIN", "4"))  # quiet mics: boost what Vapi hears


def find_device(name: str, kind: str):
    import sounddevice as sd
    for index, device in enumerate(sd.query_devices()):
        if name.lower() in device["name"].lower() and device[f"max_{kind}_channels"] > 0:
            return index
    return None


def start_audio() -> None:
    key = os.getenv("AIC_SDK_LICENSE")
    if not key:
        STATE.tyto_text = "AIC_SDK_LICENSE missing: Tyto gate is off"
        return

    def on_scores(source, scores):
        with STATE.lock:
            STATE.scores[source] = {k: round(v, 3) for k, v in scores.as_dict().items()}
            STATE.scores_at[source] = time.monotonic()

    def on_state(state, text):
        STATE.tyto_state, STATE.tyto_text = state, text
        print(f"Tyto: {text}", flush=True)

    def run():
        try:
            import contextlib
            import queue
            from functools import partial

            import aic_sdk as aic
            import sounddevice as sd
            from tyto_voice.scorer import LiveTytoScorer

            # Light smoothing: the gate must describe the command that was just spoken.
            scorers = {}
            for source in ("raw", "vf"):
                scorers[source] = LiveTytoScorer(
                    key, model_id=TYTO_MODEL, models_dir=TYTO_MODELS, sample_rate=RATE, ema_alpha=0.6,
                    on_scores=partial(on_scores, source), on_state=on_state if source == "raw" else None)
                scorers[source].start()

            model = aic.Model.from_file(aic.Model.download(VF_MODEL, str(ROOT / "models")))
            config = aic.ProcessorConfig.optimal(model, sample_rate=RATE)
            vf = aic.Processor(model, key)
            vf.initialize(config)
            block = config.block_size

            mic = find_device(os.getenv("MIC_DEVICE", "MacBook Pro Microphone"), "input")
            out = find_device(os.getenv("VF_OUTPUT_DEVICE", "BlackHole 2ch"), "output")
            inbox: queue.Queue = queue.Queue(maxsize=200)
            outbox: collections.deque = collections.deque(maxlen=40)
            primed = [False]

            def on_in(indata, _frames, _time, _status):
                try:
                    inbox.put_nowait(indata[:, 0].copy())
                except queue.Full:
                    pass

            def on_out(outdata, _frames, _time, _status):
                if not primed[0] and len(outbox) >= 3:
                    primed[0] = True
                if primed[0] and outbox:
                    outdata[:] = outbox.popleft()[:, None]
                else:
                    primed[0] = False
                    outdata.fill(0)

            sink = (sd.OutputStream(samplerate=RATE, channels=2, dtype="float32", blocksize=block,
                                    device=out, callback=on_out) if out is not None else contextlib.nullcontext())
            with sd.InputStream(samplerate=RATE, channels=1, dtype="float32", blocksize=block,
                                device=mic, callback=on_in), sink:
                mic_name = sd.query_devices(mic, "input")["name"]
                out_name = sd.query_devices(out)["name"] if out is not None else "nowhere (no BlackHole)"
                STATE.vf_text = f"{VF_MODEL}: {mic_name} -> {out_name}"
                print(f"Voice Focus {STATE.vf_text}", flush=True)
                while True:
                    raw = inbox.get()
                    STATE.level = float(np.sqrt(np.mean(raw * raw)))
                    scorers["raw"].feed(raw)
                    clean = np.asarray(vf.process(raw), dtype=np.float32)
                    scorers["vf"].feed(clean)
                    if VOICE:
                        VOICE.feed((clean if STATE.vf_on else raw) if STATE.local_voice else np.zeros_like(raw))
                    if out is not None:
                        outbox.append(np.clip((clean if STATE.vf_on else raw) * OUT_GAIN, -1, 1))
        except Exception as exc:  # noqa: BLE001
            STATE.tyto_state, STATE.tyto_text = "error", f"Audio error: {type(exc).__name__}: {exc}"
            print(STATE.tyto_text, flush=True)

    threading.Thread(target=run, name="audio", daemon=True).start()


# --------------------------------------------------------------------------- #
# Jev: act / ask / ignore                                                     #
# --------------------------------------------------------------------------- #

JEV = httpx.AsyncClient(
    base_url=os.getenv("TYPESAFE_BASE_URL", "https://api.typesafe.ai"),
    headers={"Authorization": f"Bearer {os.getenv('TYPESAFE_API_KEY', '')}"},
    timeout=httpx.Timeout(2.0, connect=4.0),
)


def band(value: float | None, low: float, high: float, words=("low", "medium", "high")) -> str:
    if value is None:
        return "unknown"
    return words[0] if value < low else words[1] if value < high else words[2]


async def ask_jev(heard: str, action: str, known: list[str], scores: dict | None) -> dict:
    """One System One round trip. Returns {choice, confidence, ms} or {error}."""
    if not os.getenv("TYPESAFE_API_KEY"):
        return {"error": "no TYPESAFE_API_KEY"}
    scores = scores or {}
    state = {
        "robot": {"what_it_is": "a robot arm on a table that people control by voice",
                  "motions_it_knows": known},
        "utterance": {"heard_words": heard or "(nothing)",
                      "motion_the_agent_wants_to_run": action or "(none)"},
        "audio": {
            "risk_of_mishearing": band(scores.get("risk_score"), 0.30, 0.50),
            "background_voices": band(scores.get("interfering_speech"), 0.15, 0.35, ("none", "some", "strong")),
            "background_noise": band(scores.get("noise"), 0.20, 0.45),
        },
    }
    questions = {
        "decision": {
            "type": "choice",
            "instructions": "A robot arm heard these words in a loud room. What should it do?",
            "criteria": {
                "act": "The words are a clear instruction for the robot arm and they match the motion the "
                       "agent wants to run. Run the motion.",
                "ask": "The words might be an instruction for the arm but they are garbled, incomplete, "
                       "do not match the motion, or the audio is too risky to trust. Ask the person to repeat.",
                "ignore": "The words are not an instruction for the robot arm, for example other people "
                          "talking to each other, small talk, or noise. Do nothing.",
            },
        },
    }
    t0 = time.perf_counter()
    try:
        r = await JEV.post("/v1/systemone", json={
            "model": os.getenv("TYPESAFE_MODEL", "jev-latest"), "state": state, "questions": questions})
        r.raise_for_status()
        answer = (r.json().get("answers") or {}).get("decision") or {}
        return {"choice": answer.get("choice"), "confidence": float(answer.get("confidence") or 0.0),
                "probabilities": answer.get("probabilities") or {}, "ms": int((time.perf_counter() - t0) * 1000)}
    except Exception as exc:  # noqa: BLE001 - any failure degrades to the rule
        return {"error": f"{type(exc).__name__}: {exc}"[:160], "ms": int((time.perf_counter() - t0) * 1000)}


# --------------------------------------------------------------------------- #
# Arm client                                                                  #
# --------------------------------------------------------------------------- #

ARM = httpx.AsyncClient(base_url=ARM_URL, timeout=5.0)


async def arm_post(path: str) -> tuple[int, dict]:
    try:
        r = await ARM.post(path)
        return r.status_code, r.json()
    except Exception as exc:  # noqa: BLE001
        return 503, {"ok": False, "reason": f"arm server unreachable ({type(exc).__name__})"}


async def poll_arm() -> None:
    import asyncio
    while True:
        try:
            STATE.arm = (await ARM.get("/health", timeout=1.5)).json()
        except Exception:  # noqa: BLE001
            STATE.arm = {"connected": False, "reason": "arm server not running"}
        await asyncio.sleep(0.5)


# --------------------------------------------------------------------------- #
# The gate                                                                    #
# --------------------------------------------------------------------------- #

async def handle_move(action: str, heard: str) -> str:
    action = (action or "").strip().lower().replace(" ", "_")
    heard = (heard or "").strip()
    # Open speaker next to the mic: the agent can hear its own short replies.
    if "".join(c for c in heard.lower() if c.isalpha() or c == " ").strip() in AGENT_PHRASES:
        STATE.event(kind="move", heard=heard, action=action, risk=None, decision="ignore", source="echo",
                    reason="the agent heard its own voice", say="")
        return "That was your own voice coming back through the microphone. Say nothing: reply with an empty message."
    known = motions()
    scores, limit = STATE.gate_scores(), STATE.limit
    risk = float(scores["risk_score"]) if scores else None

    # 1. Tyto gate: audio too risky, do not move, whatever the transcript says.
    if STATE.gate_on and risk is not None and risk >= limit:
        if scores.get("interfering_speech", 0) >= 0.35:
            say = f"Too many voices, I can't tell who is talking to me. {REPEAT}"
        elif scores.get("noise", 0) >= 0.45:
            say = f"It's too noisy, I didn't catch that. {REPEAT}"
        else:
            say = f"Your audio sounds wrong. {REPEAT}"
        STATE.event(kind="move", heard=heard, action=action, risk=risk, decision="ask", source="tyto",
                    reason=f"Tyto risk {risk:.2f} is over the limit {limit:.2f}", say=say)
        return f"NOT EXECUTED. The audio was not trustworthy. Say exactly: {say}"

    # 2. Jev decides act / ask / ignore.
    jev = await ask_jev(heard, action, known, scores)
    choice, source = jev.get("choice"), "jev"
    if "error" in jev or choice not in ("act", "ask", "ignore") or jev.get("confidence", 0) < JEV_MIN_CONFIDENCE:
        source = "rule"
        choice = "act" if action in known else "ask"
    if choice == "act" and action not in known:
        choice = "ask"
    reason = jev.get("error") or f"Jev {jev.get('choice')} {jev.get('confidence', 0):.2f} in {jev.get('ms')} ms"

    if choice == "ignore":
        STATE.event(kind="move", heard=heard, action=action, risk=risk, decision="ignore", source=source,
                    reason=reason, say="", jev=jev)
        return "NOT EXECUTED. That was not meant for the arm. Reply with only: Standing by."
    if choice == "ask":
        STATE.event(kind="move", heard=heard, action=action, risk=risk, decision="ask", source=source,
                    reason=reason, say=REPEAT, jev=jev)
        return f"NOT EXECUTED. Say exactly: {REPEAT} The arm knows these motions: {', '.join(known)}."

    # 3. Act.
    code, body = await arm_post(f"/do/{action}")
    if code != 202:
        say = "The arm is not responding."
        STATE.event(kind="move", heard=heard, action=action, risk=risk, decision="error", source="arm",
                    reason=body.get("reason", str(code)), say=say, jev=jev)
        return f"FAILED: {body.get('reason', code)}. Say exactly: {say}"
    say = "On it."
    STATE.event(kind="move", heard=heard, action=action, risk=risk, decision="act", source=source,
                reason=reason, say=say, jev=jev)
    return f"STARTED '{action}', takes about {body.get('seconds', '?')} seconds. Say only: {say}"


async def handle_stop(heard: str = "") -> str:
    code, body = await arm_post("/stop")
    scores = STATE.gate_scores()
    risk = float(scores["risk_score"]) if scores else None
    if code != 200:
        STATE.event(kind="stop", heard=heard, action="stop", risk=risk, decision="error", source="arm",
                    reason=body.get("reason", str(code)), say="The arm is not responding.")
        return "FAILED: the arm server is unreachable. Say exactly: The arm is not responding."
    STATE.event(kind="stop", heard=heard, action="stop", risk=risk, decision="stop", source="always",
                reason="stop skips the gate", say="Stopped.")
    return "STOPPED. Say only: Stopped."


# --------------------------------------------------------------------------- #
# Local voice loop (Deepgram STT/TTS, Reachy speaks and reacts)               #
# --------------------------------------------------------------------------- #

import random  # noqa: E402

from voice import LINES, STOP_WORDS, Voice, pick_action  # noqa: E402

VOICE: Voice | None = None
LOOP = None  # uvicorn's event loop, set at startup


def on_utterance(text: str) -> None:
    import asyncio
    low = text.lower()
    if any(w in low for w in STOP_WORDS):
        asyncio.run_coroutine_threadsafe(handle_stop(text), LOOP).result(timeout=6)
        decision = "stop"
    else:
        action = pick_action(text)
        asyncio.run_coroutine_threadsafe(handle_move(action, text), LOOP).result(timeout=10)
        decision = STATE.events[0]["decision"] if STATE.events else "ask"
    VOICE.reachy.react(decision)
    said = STATE.events[0].get("say", "") if STATE.events else ""
    if decision == "ask" and said:
        VOICE.say(said)  # explains what Tyto found: too many voices, noise
    elif decision in LINES:
        VOICE.say(random.choice(LINES[decision]))


# --------------------------------------------------------------------------- #
# MCP server + status routes                                                  #
# --------------------------------------------------------------------------- #

mcp = FastMCP(
    "can-you-repeat-that",
    instructions="Controls a tabletop robot arm. Use move_arm for motions and stop_arm to stop.",
    host="127.0.0.1", port=PORT, stateless_http=True, json_response=True,
    # Vapi reaches us through a tunnel, so the Host header is not localhost.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool(description=(
    "Ask the robot arm to run a motion. Call this for everything the user says that is not 'stop'. "
    f"`action` must be one of: {', '.join(motions()) or 'wave'}; use 'none' if the user's words do not "
    "clearly ask for one of them. `heard` must be the user's exact words, verbatim. "
    "The result tells you exactly what to say."
))
async def move_arm(action: str, heard: str) -> str:
    return await handle_move(action, heard)


@mcp.tool(description=(
    "Stop the robot arm immediately and hold its position. Call this the moment the user says stop, "
    "halt, freeze, wait, hold on or anything like it. Never ask for confirmation first."
))
async def stop_arm() -> str:
    return await handle_stop()


@mcp.custom_route("/state", methods=["GET"])
async def state_route(_request: Request):
    return JSONResponse(STATE.snapshot())


@mcp.custom_route("/", methods=["GET"])
async def index_route(_request: Request):
    return HTMLResponse((ROOT / "web/index.html").read_text())


@mcp.custom_route("/limit", methods=["POST"])
async def limit_route(request: Request):
    STATE.limit = min(1.0, max(0.0, float(request.query_params["v"])))
    return JSONResponse({"limit": STATE.limit})


@mcp.custom_route("/gate", methods=["POST"])
async def gate_route(request: Request):
    if request.query_params.get("v") in ("raw", "vf"):
        STATE.gate = request.query_params["v"]
    return JSONResponse({"gate": STATE.gate})


@mcp.custom_route("/switch", methods=["POST"])
async def switch_route(request: Request):
    """/switch?vf=on|off  and/or  /switch?gate=on|off"""
    q = request.query_params
    if q.get("vf") in ("on", "off"):
        STATE.vf_on = q["vf"] == "on"
    if q.get("gate") in ("on", "off"):
        STATE.gate_on = q["gate"] == "on"
    if q.get("local") in ("on", "off"):
        STATE.local_voice = q["local"] == "on"
        STATE.voice_text = "listening" if STATE.local_voice else "local voice OFF (Vapi agent mode)"
    print(f"Voice Focus {'on' if STATE.vf_on else 'OFF'}, Tyto gate {'on' if STATE.gate_on else 'OFF'}", flush=True)
    return JSONResponse({"vf_on": STATE.vf_on, "gate_on": STATE.gate_on})


@mcp.custom_route("/test/move", methods=["POST"])
async def test_move_route(request: Request):
    q = request.query_params
    return JSONResponse({"result": await handle_move(q.get("action", ""), q.get("heard", ""))})


@mcp.custom_route("/test/stop", methods=["POST"])
async def test_stop_route(_request: Request):
    return JSONResponse({"result": await handle_stop("stop")})


def main():
    import asyncio
    import contextlib

    global VOICE
    if os.getenv("LOCAL_VOICE", "1") == "1":
        VOICE = Voice(STATE, on_utterance)
    start_audio()
    app = mcp.streamable_http_app()
    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(scope):
        async with inner(scope):
            global LOOP
            LOOP = asyncio.get_running_loop()
            if VOICE:
                VOICE.start()
            task = asyncio.create_task(poll_arm())
            try:
                yield
            finally:
                task.cancel()

    app.router.lifespan_context = lifespan
    print(f"Bridge on http://127.0.0.1:{PORT}  (MCP at /mcp, screen at /)  motions: {motions()}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
