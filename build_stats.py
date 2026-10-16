#!/usr/bin/env python3
"""Baut docs/stats.json aus den Minecraft-Stats-Dateien.

Mit SFTP_HOST/SFTP_USER/SFTP_PASS (Umgebungsvariablen) werden die Dateien
vom Hoster geholt. Ohne diese Variablen liest das Skript ./raw
(UUID.json-Dateien aus world/stats + usercache.json).
"""
import json
import os
import time
from pathlib import Path

RAW, OUT = Path("raw"), Path("docs/stats.json")


def fetch_sftp():
    import paramiko
    t = paramiko.Transport((os.environ["SFTP_HOST"], int(os.environ.get("SFTP_PORT") or 22)))
    t.connect(username=os.environ["SFTP_USER"], password=os.environ["SFTP_PASS"])
    s = paramiko.SFTPClient.from_transport(t)
    world = os.environ.get("WORLD_DIR") or "world"
    RAW.mkdir(exist_ok=True)
    for f in s.listdir(f"{world}/stats"):
        if f.endswith(".json"):
            s.get(f"{world}/stats/{f}", str(RAW / f))
    try:
        s.get("usercache.json", str(RAW / "usercache.json"))
    except OSError:
        pass
    s.close()
    t.close()


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
        })

    try:
        old = json.loads(OUT.read_text("utf-8")).get("players")
    except (OSError, ValueError):
        old = None
    if old != players:  # nur schreiben, wenn sich etwas geändert hat
        OUT.parent.mkdir(exist_ok=True)
        OUT.write_text(json.dumps({"updated": int(time.time()), "players": players},
                                  ensure_ascii=False), "utf-8")
    print(len(players), "Spieler")


if __name__ == "__main__":
    if os.environ.get("SFTP_HOST"):
        fetch_sftp()
    build()
