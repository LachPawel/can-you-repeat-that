"""SO-101 follower motion server with a stop that actually stops.

Reuses the tested record/replay helpers from ~/code/reachy-booth (booth.arm.gestures)
and must run with that project's venv, which has lerobot:

    ./run.sh arm                      serve on :8092, arm port auto-detected
    ./run.sh arm-fake                 no hardware, motions are timed sleeps
    ./run.sh record hand_over 6       record by moving the arm by hand (standalone)
    curl -X POST 'localhost:8092/record/hand_over?seconds=6'   record while serving

Motions are JSON files in ./motions. A new motion preempts the running one.
Stop halts mid-motion and holds the pose the arm is in.
"""
import argparse
import glob
import json
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BOOTH = Path.home() / "code/reachy-booth"
sys.path.insert(0, str(BOOTH))
from booth.arm import gestures as g  # noqa: E402

MOTIONS = Path(__file__).resolve().parent / "motions"
EASE_SECONDS, EASE_HZ = 1.2, 30


def find_port(port=None):
    if port:
        return port
    ports = sorted(glob.glob("/dev/tty.usbmodem*"))
    if not ports:
        raise ConnectionError("no /dev/tty.usbmodem* port: is the arm plugged in and powered?")
    return ports[0]


class Arm:
    def __init__(self, port=None, robot_id=g.DEFAULT_ID, fake=False):
        self.port, self.robot_id, self.fake = port, robot_id, fake
        self.robot = None
        self.lock = threading.Lock()  # one motion at a time
        self.halt = threading.Event()
        self.playing = None
        self.last = None

    # -- connection ---------------------------------------------------------
    def ensure(self):
        if self.fake or self.robot is not None:
            return
        self.robot = g.open_robot(find_port(self.port), self.robot_id)
        print("Arm connected", flush=True)

    def alive(self):
        """Make sure the serial link works before promising a motion; reconnect once if it died."""
        if self.fake:
            return
        if self.robot is not None:
            try:
                g.positions(self.robot)
                return
            except Exception as exc:  # noqa: BLE001 - unplugged or power-cycled arm
                print(f"Arm link lost ({type(exc).__name__}), reconnecting", flush=True)
                self.drop()
        self.ensure()

    def drop(self):
        robot, self.robot = self.robot, None
        if robot is not None:
            g.close(robot)

    # -- motion -------------------------------------------------------------
    def take(self):
        """Preempt whatever is running and take the motion lock."""
        self.halt.set()
        if not self.lock.acquire(timeout=2.5):
            return False
        self.halt.clear()
        return True

    def start(self, name):
        motion = g.load(name, MOTIONS)
        if motion is None:
            return 404, {"ok": False, "reason": f"no motion '{name}'", "motions": g.names(MOTIONS)}
        if not self.take():
            return 409, {"ok": False, "reason": "the arm is busy"}
        try:
            self.alive()
        except Exception as exc:  # noqa: BLE001
            self.lock.release()
            print(f"Arm not reachable: {type(exc).__name__}: {exc}", flush=True)
            return 503, {"ok": False, "reason": f"arm not connected ({type(exc).__name__})"}
        self.playing = name
        threading.Thread(target=self.run, args=(name, motion["frames"]), daemon=True).start()
        return 202, {"ok": True, "playing": name, "seconds": round(motion["frames"][-1]["t"] + EASE_SECONDS, 1)}

    def run(self, name, frames):
        try:
            self.ensure()
            print(f"Playing {name}", flush=True)
            done = self.ease(frames[0]["positions"]) and self.replay(frames)
            if not done:
                self.hold()
                print(f"Stopped {name}", flush=True)
            self.last = {"name": name, "completed": done}
        except Exception as exc:  # noqa: BLE001
            print(f"Motion {name} failed: {type(exc).__name__}: {exc}", flush=True)
            self.last = {"name": name, "error": f"{type(exc).__name__}: {exc}"}
            self.drop()
        finally:
            self.playing = None
            self.lock.release()

    def ease(self, target):
        if self.fake:
            return not self.halt.wait(EASE_SECONDS)
        start = g.positions(self.robot)
        steps = max(1, int(EASE_SECONDS * EASE_HZ))
        for i in range(1, steps + 1):
            if self.halt.is_set():
                return False
            a = i / steps
            self.robot.send_action({k: start.get(k, v) + (v - start.get(k, v)) * a for k, v in target.items()})
            time.sleep(1 / EASE_HZ)
        return True

    def replay(self, frames):
        started = time.monotonic()
        for frame in frames:
            if self.halt.wait(max(0., started + frame["t"] - time.monotonic())):
                return False
            if not self.fake:
                self.robot.send_action(frame["positions"])
        return True

    def hold(self):
        if not self.fake and self.robot is not None:
            pose = g.positions(self.robot)
            g.retry(lambda: self.robot.send_action(pose))

    def stop(self):
        playing = self.playing
        self.halt.set()
        return 200, {"ok": True, "stopped": playing}

    # -- record while serving ----------------------------------------------
    def record(self, name, seconds):
        if not name.replace("_", "").replace("-", "").isalnum():
            return 400, {"ok": False, "reason": "name must be letters, digits, _ or -"}
        if self.fake:
            return 503, {"ok": False, "reason": "fake arm cannot record"}
        if not self.take():
            return 409, {"ok": False, "reason": "the arm is busy"}
        try:
            self.ensure()
            motion = g.record(self.robot, name, seconds=seconds)  # torque off, 3 s countdown
            g.save(motion, MOTIONS)
            pose = g.positions(self.robot)
            g.retry(lambda: self.robot.send_action(pose))
            g.retry(self.robot.bus.enable_torque)
            g.retry(lambda: self.robot.send_action(pose))
            return 200, {"ok": True, "recorded": name, "frames": len(motion["frames"])}
        except Exception as exc:  # noqa: BLE001
            self.drop()
            return 503, {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
        finally:
            self.lock.release()

    def health(self):
        return {"connected": self.fake or self.robot is not None, "fake": self.fake, "playing": self.playing,
                "last": self.last, "motions": g.names(MOTIONS)}


def server(arm, port=8092):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.reply(200, arm.health()) if self.path == "/health" else self.reply(404, {"error": "not found"})

        def do_POST(self):
            path, _, query = self.path.partition("?")
            if path == "/stop":
                return self.reply(*arm.stop())
            if path.startswith("/do/"):
                return self.reply(*arm.start(path.removeprefix("/do/")))
            if path.startswith("/record/"):
                seconds = float(urllib.parse.parse_qs(query).get("seconds", ["6"])[0])
                return self.reply(*arm.record(path.removeprefix("/record/"), seconds))
            self.reply(404, {"error": "not found"})

        def log_message(self, *args):
            pass
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description="SO-101 motions for Can You Repeat That?")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "record", "play"):
        command = sub.add_parser(name)
        if name != "serve":
            command.add_argument("name")
        command.add_argument("--port")
        command.add_argument("--id", default=g.DEFAULT_ID)
        if name == "record":
            command.add_argument("--seconds", type=float, default=6.)
        if name == "serve":
            command.add_argument("--http", type=int, default=8092)
            command.add_argument("--fake", action="store_true")
    args = parser.parse_args()
    MOTIONS.mkdir(exist_ok=True)

    if args.cmd == "record":
        robot = g.connect(find_port(args.port), args.id, torque=False)
        try:
            path = g.save(g.record(robot, args.name, seconds=args.seconds), MOTIONS)
            print(f"Saved {path}")
        finally:
            g.close(robot)
        return

    arm = Arm(args.port, args.id, fake=getattr(args, "fake", False))
    if args.cmd == "play":
        arm.ensure()
        code, body = arm.start(args.name)
        print(body)
        while arm.playing:
            time.sleep(.1)
        arm.drop()
        return

    try:
        arm.ensure()
    except Exception as exc:  # noqa: BLE001
        print(f"Arm not connected yet ({exc}); will retry on the first motion", flush=True)
    print(f"Arm server on http://127.0.0.1:{args.http}  motions: {g.names(MOTIONS)}", flush=True)
    try:
        server(arm, args.http).serve_forever()
    except KeyboardInterrupt:
        arm.stop()
        arm.drop()


if __name__ == "__main__":
    main()
