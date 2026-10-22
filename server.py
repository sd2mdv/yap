#!/usr/bin/env python3
"""
Локальный сервер курса «Языки программирования».

Раздаёт курс (index.html) студентам в локальной сети / Wi-Fi и сохраняет их результаты
в текстовые файлы папки data/:
    data/events.jsonl   — отметки присутствия, ответы на кейсы, сданные практические и лабораторные
    data/grades.jsonl   — оценки и комментарии преподавателя
    data/teacher_key.txt — ключ входа в портал преподавателя

Запуск:  python3 server.py            (порт 8000)
         python3 server.py --port 8080 --key МОЙКЛЮЧ
Нужен только Python 3.8+ — сторонние пакеты не требуются.
Код студентов на сервере не выполняется: лабораторные проверяются в браузере студента.
"""
import argparse, hmac, json, os, secrets, socket, sys, threading, time, uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
EVENTS = os.path.join(DATA, "events.jsonl")
GRADES = os.path.join(DATA, "grades.jsonl")
KEYFILE = os.path.join(DATA, "teacher_key.txt")
MAX_BODY = 256 * 1024
KINDS = {"checkin", "case", "cases", "practice", "lab"}
LOCK = threading.Lock()
STATE = {"events": [], "grades": [], "cids": set(), "key": "", "urls": []}
RATE = {}


def lan_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def load_jsonl(path):
    out = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    return out


def append_jsonl(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def clip(v, n):
    return str(v if v is not None else "")[:n]


def rate_ok(ip, limit=90, window=60):
    now = time.time()
    q = [t for t in RATE.get(ip, []) if now - t < window]
    q.append(now)
    RATE[ip] = q
    return len(q) <= limit


def norm(s):
    return " ".join(str(s or "").lower().replace("ё", "е").replace(".", " ").replace(",", " ").split())


def norm_group(s):
    return "".join(str(s or "").upper().replace("Ё", "Е").split())


class Handler(BaseHTTPRequestHandler):
    server_version = "YapCourse/2.0"

    def log_message(self, fmt, *args):
        if "/api/submit" in (args[0] if args else "") or "/api/ping" in (args[0] if args else ""):
            return
        sys.stderr.write("%s  %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            raise ValueError("Пустой или слишком большой запрос")
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def is_teacher(self):
        k = self.headers.get("X-Teacher-Key") or ""
        return bool(k) and hmac.compare_digest(k.encode(), STATE["key"].encode())

    # ---------- GET ----------
    def do_GET(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        if path == "/api/ping":
            return self.send_json(200, {"ok": True, "urls": STATE["urls"], "version": 2})
        if path == "/api/teacher/data":
            if not self.is_teacher():
                return self.send_json(403, {"error": "Неверный ключ преподавателя"})
            with LOCK:
                return self.send_json(200, {"events": STATE["events"], "grades": STATE["grades"],
                                            "files": "data/events.jsonl, data/grades.jsonl",
                                            "info": {"urls": STATE["urls"]}})
        if path == "/api/my":
            q = parse_qs(u.query)
            name, group = norm(q.get("name", [""])[0]), norm_group(q.get("group", [""])[0])
            if not name or not group:
                return self.send_json(400, {"error": "Нужны имя и группа"})
            with LOCK:
                mine = {e["id"]: e for e in STATE["events"]
                        if e.get("kind") in ("practice", "lab") and norm_group(e.get("group")) == group
                        and name in [norm(m) for m in (e.get("members") or [e.get("name")])]}
                last = {}
                for g in STATE["grades"]:
                    if g.get("ref") in mine:
                        last[g["ref"]] = g
            items = [{"lecture": mine[r]["lecture"], "kind": mine[r]["kind"], "grade": g["grade"],
                      "comment": g.get("comment", ""), "t": g["t"]} for r, g in last.items()]
            items.sort(key=lambda x: x["t"])
            return self.send_json(200, {"items": items})
        # статические файлы: только сам курс и необязательная папка runtimes/
        if path in ("/", "/index.html"):
            return self.send_file(os.path.join(BASE, "index.html"), "text/html; charset=utf-8")
        if path.startswith("/runtimes/") and ".." not in path:
            full = os.path.normpath(os.path.join(BASE, path.lstrip("/")))
            if full.startswith(os.path.join(BASE, "runtimes")) and os.path.isfile(full):
                ctype = {".js": "text/javascript", ".mjs": "text/javascript", ".wasm": "application/wasm",
                         ".json": "application/json", ".zip": "application/zip"}.get(os.path.splitext(full)[1], "application/octet-stream")
                return self.send_file(full, ctype)
        self.send_json(404, {"error": "Не найдено"})

    def send_file(self, full, ctype):
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            return self.send_json(404, {"error": "Не найдено"})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    # ---------- POST ----------
    def do_POST(self):
        path = urlparse(self.path).path
        ip = self.client_address[0]
        try:
            if path == "/api/submit":
                if not rate_ok(ip):
                    return self.send_json(429, {"error": "Слишком много запросов, подождите минуту"})
                d = self.read_json()
                kind = d.get("kind")
                if kind not in KINDS:
                    return self.send_json(400, {"error": "Неизвестный тип события"})
                if not clip(d.get("name"), 100).strip() or not clip(d.get("group"), 40).strip():
                    return self.send_json(400, {"error": "Укажите ФИО и группу"})
                ev = {
                    "id": uuid.uuid4().hex[:12], "t": int(time.time() * 1000), "ip": ip,
                    "kind": kind, "lecture": clip(d.get("lecture"), 20),
                    "name": clip(d.get("name"), 100).strip(), "group": clip(d.get("group"), 40).strip(),
                    "team": clip(d.get("team"), 80), "members": [clip(m, 100) for m in (d.get("members") or [])][:10],
                    "cid": clip(d.get("cid"), 40), "ct": d.get("ct"),
                }
                for k in ("i", "ok", "v", "score", "total", "code", "auto", "answers", "lang", "variant", "seed", "static"):
                    if k in d:
                        ev[k] = d[k]
                if kind == "lab":
                    ev["code"] = clip(d.get("code"), 40000)
                with LOCK:
                    if ev["cid"] and ev["cid"] in STATE["cids"]:
                        return self.send_json(200, {"ok": True, "duplicate": True})
                    append_jsonl(EVENTS, ev)
                    STATE["events"].append(ev)
                    STATE["cids"].add(ev["cid"])
                if kind in ("practice", "lab"):
                    print("%s  сдана %s: %s (%s), лекция %s" % (time.strftime("%H:%M:%S"),
                          "лабораторная" if kind == "lab" else "практическая", ev["name"], ev["group"], ev["lecture"]))
                return self.send_json(200, {"ok": True, "id": ev["id"]})
            if path == "/api/teacher/grade":
                if not self.is_teacher():
                    return self.send_json(403, {"error": "Неверный ключ преподавателя"})
                d = self.read_json()
                g = {"id": uuid.uuid4().hex[:12], "t": int(time.time() * 1000), "ref": clip(d.get("ref"), 20),
                     "grade": clip(d.get("grade"), 20), "comment": clip(d.get("comment"), 2000),
                     "lecture": clip(d.get("lecture"), 20), "kind": clip(d.get("kind"), 20)}
                if not g["ref"] or not g["grade"]:
                    return self.send_json(400, {"error": "Нет работы или оценки"})
                with LOCK:
                    append_jsonl(GRADES, g)
                    STATE["grades"].append(g)
                return self.send_json(200, {"ok": True, "grade": g})
            return self.send_json(404, {"error": "Не найдено"})
        except (ValueError, json.JSONDecodeError) as e:
            return self.send_json(400, {"error": str(e)})


def main():
    ap = argparse.ArgumentParser(description="Локальный сервер курса «Языки программирования»")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--key", help="ключ преподавателя (по умолчанию — из data/teacher_key.txt или новый)")
    a = ap.parse_args()
    os.makedirs(DATA, exist_ok=True)
    if not os.path.exists(os.path.join(BASE, "index.html")):
        sys.exit("Не найден index.html рядом с server.py")
    if a.key:
        key = a.key.strip()
    elif os.path.exists(KEYFILE):
        key = open(KEYFILE, encoding="utf-8").read().strip()
    else:
        key = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
    with open(KEYFILE, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    STATE["key"] = key
    STATE["events"] = load_jsonl(EVENTS)
    STATE["grades"] = load_jsonl(GRADES)
    STATE["cids"] = {e.get("cid") for e in STATE["events"] if e.get("cid")}
    ips = lan_ips()
    STATE["urls"] = ["http://%s:%d/" % (ip, a.port) for ip in ips]
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    except OSError as e:
        sys.exit("Порт %d занят (%s). Запустите с другим портом: python3 server.py --port 8080" % (a.port, e))
    line = "=" * 64
    print(line)
    print(" Курс «Языки программирования» — сервер запущен")
    print(line)
    print(" Студентам (та же Wi-Fi / локальная сеть):")
    for u in STATE["urls"] or ["http://<IP-этого-компьютера>:%d/" % a.port]:
        print("    " + u)
    print(" На этом компьютере:      http://localhost:%d/" % a.port)
    print(" Портал преподавателя:    откройте адрес → «Портал преподавателя»")
    print(" Ключ преподавателя:      " + key)
    print(" Результаты сохраняются:  " + DATA)
    print("    событий: %d, оценок: %d" % (len(STATE["events"]), len(STATE["grades"])))
    print(" Остановить сервер: Ctrl+C")
    print(line)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен. Данные сохранены в папке data/.")


if __name__ == "__main__":
    main()
