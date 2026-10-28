#!/usr/bin/env python3
"""Remote controller for a QEMU VM: reads commands from git branch lab10-ctl,
pushes screenshots to branch lab10-out."""
import json, os, socket, subprocess, time, shutil, sys

QMP = "/tmp/qmp.sock"
WORK = os.environ.get("WORK", "/mnt/vm")
OUT = os.path.join(WORK, "out")
REPO = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GH_TOKEN"]
REMOTE = f"https://x-access-token:{TOKEN}@github.com/{REPO}.git"

os.makedirs(OUT, exist_ok=True)
os.makedirs(os.path.join(OUT, "shots"), exist_ok=True)


class Qmp:
    def __init__(self):
        self.s = socket.socket(socket.AF_UNIX)
        self.s.connect(QMP)
        self.f = self.s.makefile("rw")
        self.f.readline()
        self.cmd("qmp_capabilities")

    def cmd(self, name, **args):
        self.f.write(json.dumps({"execute": name, "arguments": args}) + "\n")
        self.f.flush()
        while True:
            line = self.f.readline()
            if not line:
                raise RuntimeError("qmp closed")
            r = json.loads(line)
            if "return" in r or "error" in r:
                return r


SHIFT = {}
for a, b in zip("~!@#$%^&*()_+{}|:\"<>?", "`1234567890-=[]\\;',./"):
    SHIFT[a] = b
NAMES = {"`": "grave_accent", "1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "6": "6",
         "7": "7", "8": "8", "9": "9", "0": "0", "-": "minus", "=": "equal", "[": "bracket_left",
         "]": "bracket_right", "\\": "backslash", ";": "semicolon", "'": "apostrophe", ",": "comma",
         ".": "dot", "/": "slash", " ": "spc", "\n": "ret", "\t": "tab"}


def char_keys(c):
    if c.isalpha() and c.isascii():
        return (["shift"] if c.isupper() else []) + [c.lower()]
    if c in SHIFT:
        return ["shift", NAMES[SHIFT[c]]]
    return [NAMES[c]]


def key(q, combo, hold=60):
    keys = [{"type": "qcode", "data": k} for k in combo]
    q.cmd("send-key", keys=keys, **{"hold-time": hold})


def screenshot(q, path):
    q.cmd("screendump", filename="/tmp/scr.ppm")
    time.sleep(0.3)
    subprocess.run(["convert", "/tmp/scr.ppm", path], check=False)


def mouse(q, x, y, btn=None, double=False, right=False, w=1024, h=768):
    ax, ay = int(x * 32767 / (w - 1)), int(y * 32767 / (h - 1))
    q.cmd("input-send-event", events=[{"type": "abs", "data": {"axis": "x", "value": ax}},
                                      {"type": "abs", "data": {"axis": "y", "value": ay}}])
    if btn is None:
        return
    b = "right" if right else "left"
    for _ in range(2 if double else 1):
        q.cmd("input-send-event", events=[{"type": "btn", "data": {"down": True, "button": b}}])
        time.sleep(0.08)
        q.cmd("input-send-event", events=[{"type": "btn", "data": {"down": False, "button": b}}])
        time.sleep(0.12)


def sh(*a, **k):
    return subprocess.run(list(a), **k)


def git_setup():
    os.makedirs("/tmp/ctl", exist_ok=True)
    sh("git", "init", "-q", "/tmp/ctl")
    sh("git", "-C", "/tmp/ctl", "remote", "add", "origin", REMOTE)
    os.makedirs("/tmp/outrepo", exist_ok=True)
    sh("git", "init", "-q", "/tmp/outrepo")
    sh("git", "-C", "/tmp/outrepo", "config", "user.email", "bot@example.com")
    sh("git", "-C", "/tmp/outrepo", "config", "user.name", "lab-bot")


def fetch_cmds():
    r = sh("git", "-C", "/tmp/ctl", "fetch", "-q", "--depth", "1", "origin", "lab10-ctl",
           capture_output=True)
    if r.returncode != 0:
        return []
    r = sh("git", "-C", "/tmp/ctl", "show", "FETCH_HEAD:cmds.jsonl", capture_output=True, text=True)
    out = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def push_out(status):
    d = "/tmp/outrepo"
    for f in os.listdir(d):
        if f != ".git":
            p = os.path.join(d, f)
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
    shutil.copytree(OUT, d, dirs_exist_ok=True)
    with open(os.path.join(d, "status.json"), "w") as fh:
        json.dump(status, fh, indent=1)
    shutil.rmtree(os.path.join(d, ".git"), ignore_errors=True)
    sh("git", "init", "-q", d)
    sh("git", "-C", d, "add", "-A")
    sh("git", "-C", d, "-c", "user.email=bot@example.com", "-c", "user.name=lab-bot",
       "commit", "-q", "-m", "out")
    sh("git", "-C", d, "push", "-q", "-f", REMOTE, "HEAD:refs/heads/lab10-out")


def run(q, c, log):
    op = c["op"]
    if op == "key":
        for combo in c["keys"]:
            key(q, combo if isinstance(combo, list) else [combo])
            time.sleep(c.get("gap", 0.25))
    elif op == "type":
        for ch in c["text"]:
            key(q, char_keys(ch), hold=30)
            time.sleep(c.get("gap", 0.04))
    elif op == "click":
        mouse(q, c["x"], c["y"], btn=True, double=c.get("double", False), right=c.get("right", False))
    elif op == "move":
        mouse(q, c["x"], c["y"])
    elif op == "shot":
        screenshot(q, os.path.join(OUT, "shots", c["name"] + ".png"))
    elif op == "wait":
        time.sleep(c["s"])
    elif op == "hmp":
        r = q.cmd("human-monitor-command", **{"command-line": c["cmd"]})
        log.append(str(r)[:500])
    elif op == "shell":
        r = sh("bash", "-c", c["cmd"], capture_output=True, text=True, timeout=600)
        log.append((r.stdout + r.stderr)[-3000:])
    elif op == "quit":
        return "quit"


def main():
    git_setup()
    for _ in range(60):
        try:
            q = Qmp()
            break
        except Exception:
            time.sleep(1)
    done = set()
    log = []
    last_push = 0
    start = time.time()
    while True:
        cmds = fetch_cmds()
        new = [c for c in cmds if c["id"] not in done]
        quit_ = False
        for c in new:
            try:
                if run(q, c, log) == "quit":
                    quit_ = True
            except Exception as e:
                log.append(f"err {c.get('id')}: {e}")
                try:
                    q = Qmp()
                except Exception:
                    pass
            done.add(c["id"])
            log.append(f"done {c['id']}")
        log = log[-40:]
        try:
            screenshot(q, os.path.join(OUT, "screen.png"))
        except Exception as e:
            log.append(f"shot err {e}")
            try:
                q = Qmp()
            except Exception:
                pass
        if new or time.time() - last_push > 25:
            df = sh("df", "-h", WORK, capture_output=True, text=True).stdout
            push_out({"t": time.strftime("%H:%M:%S"), "up": int(time.time() - start),
                      "done": sorted(done)[-20:], "log": log, "df": df})
            last_push = time.time()
        if quit_:
            break
        time.sleep(5)


if __name__ == "__main__":
    main()
