"""Local voice loop, no browser: Deepgram STT in, Deepgram TTS out of Reachy's speaker.

    mic -> Voice Focus 2.3 -> Deepgram nova-3 (streaming) -> gate (Tyto + Jev) -> SO-101
                                                         -> reply via Deepgram Aura -> Reachy speaker
                                                         -> Reachy head + antennas react

Half duplex: while Reachy speaks, silence is sent to Deepgram so it cannot hear itself.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time

import httpx
import numpy as np

RATE = 16000
TTS_RATE = 24000
REACHY = os.getenv("REACHY_URL", "http://127.0.0.1:8000")

STOP_WORDS = ("stop", "halt", "freeze", "hold on", "wait", "enough")
ACTIONS = {  # spoken keyword -> motion file
    "wave": "wave", "hello": "wave", "hi ": "wave", "hey": "wave",
    "point": "point_person", "show me": "point_person",
    "rest": "rest", "relax": "rest", "sleep": "rest", "down": "rest", "home": "rest",
}
LINES = {
    "act": ["On it!", "Here we go!", "Watch this!"],
    "ask": ["Can you repeat that?"],
    "stop": ["Stopped!"],
    "error": ["The arm is not responding."],
}


def pick_action(text: str) -> str:
    t = f" {text.lower()} "
    for word, action in ACTIONS.items():
        if word in t:
            return action
    return "none"


class Reachy:
    """Head and antenna reactions over the Reachy Mini daemon REST API."""

    def __init__(self):
        self.http = httpx.Client(base_url=REACHY, timeout=2.0)

    def goto(self, duration=0.5, antennas=(0.0, 0.0), roll=0.0, pitch=0.0, yaw=0.0):
        try:
            self.http.post("/api/move/goto", json={
                "head_pose": {"x": 0, "y": 0, "z": 0, "roll": roll, "pitch": pitch, "yaw": yaw},
                "antennas": list(antennas), "duration": duration})
        except Exception:  # noqa: BLE001 - Reachy is decoration, never block the arm
            pass

    def react(self, decision: str):
        def run():
            if decision == "act":  # happy nod, antennas up
                self.goto(0.35, (0.6, -0.6), pitch=0.25); time.sleep(0.4)
                self.goto(0.35, (0.6, -0.6), pitch=-0.15); time.sleep(0.4)
                self.goto(0.5)
            elif decision == "ask":  # puzzled tilt, one antenna down
                self.goto(0.5, (0.9, 0.2), roll=0.3); time.sleep(1.6)
                self.goto(0.6)
            elif decision == "stop":  # freeze, antennas flat
                self.goto(0.3, (-0.4, 0.4), pitch=0.1); time.sleep(1.0)
                self.goto(0.6)
            elif decision == "listen":
                self.goto(0.4, (0.25, -0.25), pitch=-0.05)
        threading.Thread(target=run, daemon=True).start()

    def wiggle(self, seconds: float):
        def run():
            end = time.monotonic() + seconds
            flip = 1
            while time.monotonic() < end:
                self.goto(0.18, (0.35 * flip, 0.35 * flip))
                flip = -flip
                time.sleep(0.2)
            self.goto(0.3)
        threading.Thread(target=run, daemon=True).start()


class Voice:
    def __init__(self, state, on_text):
        self.state = state
        self.on_text = on_text  # called with a final utterance, on a worker thread
        self.key = os.getenv("DEEPGRAM_API_KEY", "")
        self.voice = os.getenv("TTS_VOICE", "aura-2-arcas-en")
        self.pitch = float(os.getenv("TTS_PITCH", "1.22"))  # >1 = faster and higher: cartoon
        self.audio_q: queue.Queue = queue.Queue(maxsize=400)
        self.speaking_until = 0.0
        self.reachy = Reachy()
        self.out_device = None
        self.text = ""
        self.lock = threading.Lock()  # one reply at a time

    # -- audio in -----------------------------------------------------------
    @property
    def speaking(self) -> bool:
        return time.monotonic() < self.speaking_until

    def feed(self, mono: np.ndarray):
        if self.speaking:
            mono = np.zeros_like(mono)  # half duplex, keep the stream alive
        pcm = (np.clip(mono, -1, 1) * 32767).astype(np.int16).tobytes()
        try:
            self.audio_q.put_nowait(pcm)
        except queue.Full:
            pass

    # -- Deepgram streaming STT --------------------------------------------
    def start(self):
        if not self.key:
            print("Voice: DEEPGRAM_API_KEY missing, local voice loop off", flush=True)
            return
        import sounddevice as sd
        name = os.getenv("SPEAKER_DEVICE", "Reachy Mini Audio")
        for i, d in enumerate(sd.query_devices()):
            if name.lower() in d["name"].lower() and d["max_output_channels"] > 0:
                self.out_device = i
        print(f"Voice: speaker = {sd.query_devices(self.out_device)['name'] if self.out_device is not None else 'default'}",
              flush=True)
        threading.Thread(target=self._run, name="deepgram", daemon=True).start()
        threading.Thread(target=lambda: (time.sleep(1.5), self.say("Arm is ready!")), daemon=True).start()

    def _run(self):
        from websockets.sync.client import connect
        url = ("wss://api.deepgram.com/v1/listen?model=nova-3&language=en&encoding=linear16"
               f"&sample_rate={RATE}&channels=1&interim_results=true&endpointing=300"
               "&utterance_end_ms=1000&smart_format=true&vad_events=true")
        while True:
            try:
                with connect(url, additional_headers={"Authorization": f"Token {self.key}"},
                             open_timeout=10, max_size=None) as ws:
                    print("Voice: Deepgram connected", flush=True)
                    self.state.voice_text = "listening"
                    stop = threading.Event()

                    def send():
                        while not stop.is_set():
                            try:
                                ws.send(self.audio_q.get(timeout=1))
                            except queue.Empty:
                                ws.send(json.dumps({"type": "KeepAlive"}))
                            except Exception:  # noqa: BLE001
                                return
                    sender = threading.Thread(target=send, daemon=True)
                    sender.start()
                    try:
                        for raw in ws:
                            self._on_message(json.loads(raw))
                    finally:
                        stop.set()
            except Exception as exc:  # noqa: BLE001
                print(f"Voice: Deepgram error {type(exc).__name__}: {exc}; reconnecting", flush=True)
                self.state.voice_text = f"Deepgram error: {exc}"[:120]
                time.sleep(1.0)

    def _on_message(self, msg: dict):
        kind = msg.get("type")
        if kind == "Results":
            alt = (msg.get("channel") or {}).get("alternatives") or [{}]
            words = (alt[0].get("transcript") or "").strip()
            if not words:
                if msg.get("speech_final"):
                    self._flush()
                return
            self.state.partial = (self.text + " " + words).strip()
            # Stop must not wait for the end of the sentence.
            if any(w in f" {words.lower()} " for w in (" stop", "halt", "freeze")):
                self.text = ""
                self.state.partial = ""
                self._dispatch(words)
                return
            if msg.get("is_final"):
                self.text = (self.text + " " + words).strip()
                if msg.get("speech_final"):
                    self._flush()
        elif kind == "UtteranceEnd":
            self._flush()

    def _flush(self):
        text, self.text = self.text.strip(), ""
        self.state.partial = ""
        if text and not self.speaking:
            self._dispatch(text)

    def _dispatch(self, text: str):
        print(f"Heard: {text!r}", flush=True)
        threading.Thread(target=self.on_text, args=(text,), daemon=True).start()

    # -- Deepgram TTS out of Reachy ----------------------------------------
    def say(self, text: str):
        if not text or not self.key:
            return
        with self.lock:
            try:
                r = httpx.post(
                    f"https://api.deepgram.com/v1/speak?model={self.voice}&encoding=linear16"
                    f"&sample_rate={TTS_RATE}&container=none",
                    headers={"Authorization": f"Token {self.key}"}, json={"text": text}, timeout=8)
                r.raise_for_status()
                pcm = np.frombuffer(r.content, dtype=np.int16).astype(np.float32) / 32768
                self._play(pcm)
            except Exception as exc:  # noqa: BLE001
                print(f"Voice: TTS failed {type(exc).__name__}: {exc}", flush=True)

    def _play(self, pcm: np.ndarray):
        import sounddevice as sd
        rate = int(TTS_RATE * self.pitch)
        seconds = len(pcm) / rate
        self.speaking_until = time.monotonic() + seconds + 0.6  # echo tail
        self.reachy.wiggle(seconds)
        stereo = np.repeat(pcm[:, None], 2, axis=1)
        for device in (self.out_device, None):
            try:
                sd.play(stereo, samplerate=rate, device=device, blocking=True)
                return
            except Exception as exc:  # noqa: BLE001 - fall back to the default speaker
                print(f"Voice: playback on {device} failed ({exc}), using default", flush=True)
                if device is None:
                    raise
