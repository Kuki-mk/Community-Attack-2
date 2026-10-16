#!/usr/bin/env python3
"""Baut docs/stats.json: Stats-Dateien (per SFTP, FTP oder aus ./raw) plus Online-Status per Server-Ping."""
import calendar
import ftplib
import json
import os
import posixpath
import re
import socket
import struct
import time
from pathlib import Path

RAW, OUT = Path("raw"), Path("docs/stats.json")
NUM = ("h", "d", "m", "j", "sw", "w", "f", "mk", "ts", "dt", "sl", "v", "dd", "tt", "sn", "sk", "fl", "tr", "pl", "ac", "ls")
# Items, die beim Benutzen KEIN Block werden (für die Schätzung "platzierte Blöcke")
NOBLOCK = re.compile(r"(_pickaxe|_axe|_shovel|_hoe|_sword|_helmet|_chestplate|_leggings|_boots|_spawn_egg|_bucket|_potion|_boat|_minecart|_stew|_soup|_bottle|_dye|_template|_pattern|_horse_armor)$|^(bow|crossbow|trident|mace|shield|shears|flint_and_steel|fishing_rod|brush|spyglass|compass|clock|lead|name_tag|saddle|elytra|firework_rocket|ender_pearl|ender_eye|snowball|egg|experience_bottle|fire_charge|wind_charge|bone_meal|map|filled_map|book|writable_book|enchanted_book|paper|stick|totem_of_undying|golden_apple|enchanted_golden_apple|apple|bread|cookie|beef|porkchop|chicken|mutton|rabbit|cod|salmon|tropical_fish|pufferfish|rotten_flesh|spider_eye|golden_carrot|melon_slice|chorus_fruit|potion|arrow|spectral_arrow|tipped_arrow|bowl|bundle|goat_horn|cooked_.*|music_disc_.*)$")


def fetch_sftp():
    import paramiko
    t = paramiko.Transport((os.environ["SFTP_HOST"], int(os.environ.get("SFTP_PORT") or 22)))
    t.connect(username=os.environ["SFTP_USER"], password=os.environ["SFTP_PASS"])
    s = paramiko.SFTPClient.from_transport(t)
    world = os.environ.get("WORLD_DIR") or "world"
    RAW.mkdir(exist_ok=True)
    for a in s.listdir_attr(f"{world}/stats"):
        if a.filename.endswith(".json"):
            dst = RAW / a.filename
            s.get(f"{world}/stats/{a.filename}", str(dst))
            if a.st_mtime:  # Änderungszeit = Zeitpunkt des letzten Speicherns (≈ Logout)
                os.utime(dst, (a.st_mtime, a.st_mtime))
    try:
        s.get(posixpath.join(posixpath.dirname(world), "usercache.json"), str(RAW / "usercache.json"))
    except OSError:
        pass
    s.close()
    t.close()


def list_date(p):
    """'Oct 16 14:32' oder 'Oct 16 2025' (LIST-Format) -> Unix-Zeit oder None."""
    try:
        if ":" in p[2]:
            y = time.gmtime().tm_year
            ts = calendar.timegm(time.strptime(f"{p[0]} {p[1]} {p[2]} {y}", "%b %d %H:%M %Y"))
            if ts > time.time() + 86400:
                ts = calendar.timegm(time.strptime(f"{p[0]} {p[1]} {p[2]} {y - 1}", "%b %d %H:%M %Y"))
            return ts
        return calendar.timegm(time.strptime(" ".join(p), "%b %d %Y"))
    except ValueError:
        return None


def ftp_list(ftp):
    """Dateien per LIST lesen (manche Hoster kennen weder NLST noch MLSD). -> {name: zeit|None}"""
    lines, out = [], {}
    ftp.retrlines("LIST", lines.append)
    for ln in lines:
        parts = ln.split()
        if parts and parts[-1].endswith(".json") and not ln.startswith("d"):
            out[parts[-1]] = list_date(parts[5:8]) if len(parts) >= 9 else None
    return out


SKIP = {"libraries", "versions", "plugins", "logs", "cache", "config", "crash-reports", "backups"}


def find_stats(ftp, home):
    """Sucht den Ordner 'stats' (z. B. world/stats oder <Welt>/players/stats). Danach steht ftp darin."""
    want = os.environ.get("WORLD_DIR")
    queue = ([(want.strip("/"), 0)] if want else []) + [("", 0)]
    seen, root = set(), []
    while queue:
        path, depth = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        lines = []
        try:
            ftp.cwd(home)
            if path:
                ftp.cwd(path)
            ftp.retrlines("LIST", lines.append)
        except ftplib.error_perm:
            continue
        if path == "":
            root = lines
        if posixpath.basename(path) == "stats" and any(l.split()[-1].endswith(".json") for l in lines if l.split()):
            return path
        if depth >= 3:
            continue
        for l in lines:
            p = l.split()
            if p and l.startswith("d") and p[-1] not in SKIP and not p[-1].startswith("."):
                queue.append((f"{path}/{p[-1]}".lstrip("/"), depth + 1))
    print(f"Kein Ordner 'stats' mit .json-Dateien gefunden. Inhalt von '{home}':")
    print("\n".join(root))
    raise SystemExit("Stats-Ordner nicht gefunden – bitte WORLD_DIR auf den Welt-Ordner setzen")


def ftp_download(ftp):
    home = ftp.pwd()
    stats = find_stats(ftp, home)
    print("Stats-Ordner gefunden:", stats)
    RAW.mkdir(exist_ok=True)
    mt = {}
    try:  # Änderungszeiten (UTC), falls der Server MLSD kann
        for n, facts in ftp.mlsd(facts=["modify"]):
            if n.endswith(".json") and "modify" in facts:
                mt[n] = facts["modify"]
    except ftplib.all_errors:
        pass
    info = {} if mt else ftp_list(ftp)
    for n in list(mt) or list(info):
        dst = RAW / n
        with open(dst, "wb") as fh:
            ftp.retrbinary("RETR " + n, fh.write)
        m, ts = mt.get(n), None
        if not m:
            try:
                m = ftp.voidcmd("MDTM " + n)[4:].strip()
            except ftplib.all_errors:
                m = None
        if m:
            ts = calendar.timegm(time.strptime(m[:14], "%Y%m%d%H%M%S"))
        ts = ts or info.get(n) or 0  # 0 = unbekannt
        os.utime(dst, (ts, ts))
    try:  # Fortschritte liegen neben dem stats-Ordner
        ftp.cwd(home)
        ftp.cwd(posixpath.join(posixpath.dirname(stats), "advancements"))
        (RAW / "adv").mkdir(exist_ok=True)
        for n in ftp_list(ftp):
            with open(RAW / "adv" / n, "wb") as fh:
                ftp.retrbinary("RETR " + n, fh.write)
    except ftplib.all_errors:
        print("Fortschritte nicht gefunden (übersprungen)")
    parts = stats.split("/")  # usercache.json liegt im Server-Hauptordner: von innen nach außen suchen
    for k in range(len(parts) - 1, -1, -1):
        buf = []
        try:
            ftp.cwd(home)
            if k:
                ftp.cwd("/".join(parts[:k]))
            ftp.retrbinary("RETR usercache.json", buf.append)
            (RAW / "usercache.json").write_bytes(b"".join(buf))
            break
        except ftplib.all_errors:
            continue


def fetch_ftp():
    """Erst verschlüsselt (FTPS), sonst als normales FTP."""
    miss = [k for k in ("FTP_HOST", "FTP_USER", "FTP_PASS") if not os.environ.get(k)]
    if miss:
        raise SystemExit("Fehlende Secrets: " + ", ".join(miss))
    host, port = os.environ["FTP_HOST"], int(os.environ.get("FTP_PORT") or 21)
    for cls in (ftplib.FTP_TLS, ftplib.FTP):
        try:
            ftp = cls()
            ftp.connect(host, port, timeout=30)
            ftp.login(os.environ["FTP_USER"], os.environ["FTP_PASS"])
            if cls is ftplib.FTP_TLS:
                ftp.prot_p()
            ftp_download(ftp)
            ftp.close()
            print("FTP-Verbindung:", cls.__name__)
            return
        except ftplib.all_errors as e:
            print(cls.__name__, "fehlgeschlagen:", e)
    raise SystemExit("FTP-Abruf fehlgeschlagen")


def varint(n):
    out = b""
    while True:
        b, n = n & 0x7F, n >> 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def read_varint(s):
    n = 0
    for i in range(5):
        b = s.recv(1)[0]
        n |= (b & 0x7F) << (7 * i)
        if not b & 0x80:
            return n
    raise ValueError("varint")


def ping(host, port):
    """Minecraft Server-List-Ping. Liefert das Status-JSON oder None."""
    try:
        with socket.create_connection((host, port), timeout=8) as s:
            h = host.encode()
            data = b"\x00" + varint(765) + varint(len(h)) + h + struct.pack(">H", port) + varint(1)
            s.sendall(varint(len(data)) + data + b"\x01\x00")
            read_varint(s)
            read_varint(s)
            n, buf = read_varint(s), b""
            while len(buf) < n:
                chunk = s.recv(n - len(buf))
                if not chunk:
                    break
                buf += chunk
            return json.loads(buf)
    except (OSError, ValueError, IndexError):
        return None


def build():
    names = {}
    try:
        for u in json.loads((RAW / "usercache.json").read_text("utf-8")):
            names[u["uuid"].replace("-", "")] = u["name"]
    except (OSError, ValueError):
        pass

    players = []
    for f in sorted(RAW.glob("*-*.json")):
        try:
            st = json.loads(f.read_text("utf-8")).get("stats", {})
        except ValueError:
            continue
        uid = f.stem.replace("-", "")
        cu = st.get("minecraft:custom", {})
        g = lambda k: cu.get("minecraft:" + k, 0)
        hours = (g("play_time") or g("play_one_minute")) / 72000
        if hours == 0:
            continue
        used = st.get("minecraft:used", {})
        ac = 0
        try:
            adv = json.loads((RAW / "adv" / f.name).read_text("utf-8"))
            ac = sum(1 for k, v in adv.items() if isinstance(v, dict) and v.get("done") and "recipes/" not in k)
        except (OSError, ValueError):
            pass
        players.append({
            "n": names.get(uid, "Spieler " + uid[:6]),
            "h": round(hours, 1),
            "d": g("deaths"),
            "m": sum(st.get("minecraft:mined", {}).values()),
            "j": g("jump"),
            "sw": round(g("swim_one_cm") / 1e5, 2),
            "w": round((g("walk_one_cm") + g("sprint_one_cm")) / 1e5, 1),
            "f": round(g("aviate_one_cm") / 1e5, 1),
            "mk": g("mob_kills"),
            "ts": round(g("time_since_death") / 72000, 1),
            "dt": round(g("damage_taken") / 20),
            "sl": g("sleep_in_bed"),
            "v": g("talked_to_villager"),
            "dd": round(g("damage_dealt") / 20),
            "tt": used.get("minecraft:totem_of_undying", 0),
            "sn": round(g("crouch_one_cm") / 1e5, 2),
            "sk": round(g("sneak_time") / 72000, 1),
            "fl": round(g("fall_one_cm") / 1e5, 2),
            "tr": round(g("time_since_rest") / 72000, 1),
            "pl": sum(v for k, v in used.items() if not NOBLOCK.search(k[10:])),
            "ac": ac,
            "last": int(f.stat().st_mtime) or None,  # None = unbekannt
        })

    try:
        old = json.loads(OUT.read_text("utf-8"))
    except (OSError, ValueError):
        old = {}
    prev = {p["n"]: p for p in old.get("players", [])}

    # Online-Status (nur wenn MC_HOST gesetzt ist)
    now, server = int(time.time()), None
    host = os.environ.get("MC_HOST")
    if host:
        info = ping(host, int(os.environ.get("MC_PORT") or 25565))
        pl = (info or {}).get("players", {})
        online = {x["name"] for x in pl.get("sample", [])}
        server = {"on": info is not None, "count": pl.get("online", 0), "max": pl.get("max", 0)}
        known = {p["n"] for p in players}
        players += [{"n": n, **{k: 0 for k in NUM}} for n in sorted(online - known)]
        for p in players:
            if p["n"] in online:
                o = prev.get(p["n"], {})
                p["on"] = True
                p["since"] = (o.get("since") if o.get("on") else None) or now
                p["ls"] = round(max(o.get("ls", 0), (now - p["since"]) / 3600), 2)  # längste Session (Std.)
                p.pop("last", None)
            else:
                p["on"] = False
                p["ls"] = prev.get(p["n"], {}).get("ls", 0)

    if (old.get("players"), old.get("server")) != (players, server):  # nur bei Änderung schreiben
        OUT.parent.mkdir(exist_ok=True)
        OUT.write_text(json.dumps({"updated": now, "server": server, "players": players},
                                  ensure_ascii=False), "utf-8")
    print(len(players), "Spieler")


if __name__ == "__main__":
    if os.environ.get("FTP_HOST"):  # FTP hat Vorrang, übrig gebliebene SFTP-Secrets stören nicht
        fetch_ftp()
    elif os.environ.get("SFTP_HOST"):
        fetch_sftp()
    build()
