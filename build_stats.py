#!/usr/bin/env python3
"""Baut docs/stats.json: Stats-Dateien (per SFTP, FTP oder aus ./raw) plus Online-Status per Server-Ping."""
import calendar
import ftplib
import json
import os
import posixpath
import socket
import struct
import time
from pathlib import Path

RAW, OUT = Path("raw"), Path("docs/stats.json")
NUM = ("h", "d", "m", "j", "sw", "w", "f", "mk", "ts", "dt", "sl", "v")


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


def ftp_download(ftp):
    world = os.environ.get("WORLD_DIR") or "world"
    home = ftp.pwd()
    RAW.mkdir(exist_ok=True)
    try:
        ftp.cwd(world + "/stats")
    except ftplib.error_perm:
        ftp.cwd(home)
        print(f"Ordner '{world}/stats' nicht gefunden. Inhalt von '{home}':", ftp.nlst())
        raise
    mt = {}
    try:  # Änderungszeiten (UTC), falls der Server MLSD kann
        for n, facts in ftp.mlsd(facts=["modify"]):
            if n.endswith(".json") and "modify" in facts:
                mt[n] = facts["modify"]
    except ftplib.all_errors:
        pass
    names = list(mt) or [posixpath.basename(n) for n in ftp.nlst() if n.endswith(".json")]
    for n in names:
        dst = RAW / n
        with open(dst, "wb") as fh:
            ftp.retrbinary("RETR " + n, fh.write)
        m = mt.get(n)
        if not m:
            try:
                m = ftp.voidcmd("MDTM " + n)[4:].strip()
            except ftplib.all_errors:
                m = None
        if m:
            ts = calendar.timegm(time.strptime(m[:14], "%Y%m%d%H%M%S"))
            os.utime(dst, (ts, ts))
    ftp.cwd(home)
    base = posixpath.dirname(world)
    if base:
        ftp.cwd(base)
    try:
        with open(RAW / "usercache.json", "wb") as fh:
            ftp.retrbinary("RETR usercache.json", fh.write)
    except ftplib.all_errors:
        pass


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
            "last": int(f.stat().st_mtime),
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
                p.pop("last", None)
            else:
                p["on"] = False

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
