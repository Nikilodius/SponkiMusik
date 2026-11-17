# -*- coding: utf-8 -*-
"""
Sponki — десктоп-плеер без авторизации.
Поиск и стриминг через yt-dlp. Лайки, плейлисты и «Моя волна» хранятся локально.
"""

import json
import os
import random
import re
import sys
import threading
import time
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import webview
from yt_dlp import YoutubeDL

# при работе из exe (PyInstaller) файлы лежат во временном распакованном каталоге
def _base_dir() -> str:
    if hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _base_dir()


def _data_dir() -> str:
    """Папка данных. ВАЖНО: всегда рядом с exe, никогда во временной папке PyInstaller."""
    if getattr(sys, "frozen", False):
        # путь к самому exe-файлу
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _data_file() -> str:
    return os.path.join(_data_dir(), "data.json")


DATA_DIR = _data_dir()
DATA_FILE = _data_file()

# кэш прямых ссылок на потоки: video_id -> (url, время получения)
# ссылки живут ~6 часов, кэшируем на 2 часа, чтобы треки запускались мгновенно
_stream_cache = {}
_STREAM_TTL = 2 * 60 * 60

# кэш текстов песен: video_id -> результат
_lyrics_cache = {}

# кэш поиска: запрос -> (результат, время)
_search_cache = {}
_SEARCH_TTL = 30 * 60

YDL_BASE = {
    'quiet': True,
    'skip_download': True,
    'noplaylist': True,
    'nocheckcertificate': True,
}


# ---------------------------------------------------------------------------
# Локальное хранилище (data.json)
# ---------------------------------------------------------------------------

def _load() -> dict:
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"liked": [], "playlists": [], "history": []}


def _save(d: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def _clean_track(t: dict) -> dict:
    """Оставляем только нужные поля трека для хранения."""
    return {
        "id": t.get("id"),
        "title": t.get("title"),
        "artists": t.get("artists"),
        "cover": t.get("cover"),
        "duration_ms": t.get("duration_ms"),
        "source": t.get("source", "yt"),
    }


class Api:
    # ---------------------------- Поиск ----------------------------

    def yt_search(self, text: str) -> dict:
        """Поиск музыки на YouTube (страница результатов, фильтр «только видео»). С кэшем 30 мин."""
        cached = _search_cache.get(text)
        if cached and time.time() - cached[1] < _SEARCH_TTL:
            return {"ok": True, **cached[0], "cached": True}

        q = urllib.parse.quote(text)
        url = f"https://www.youtube.com/results?search_query={q}&sp=EgIQAQ%253D%253D"
        opts = dict(YDL_BASE)
        opts['extract_flat'] = True
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            return {"ok": False, "error": str(e)}

        tracks = []
        for e in (info or {}).get("entries") or []:
            if not e:
                continue
            duration = e.get("duration")
            if duration is not None and (duration < 60 or duration > 900):
                continue  # не трек: слишком короткое или микс/сет
            thumbs = e.get("thumbnails") or []
            thumb = None
            if thumbs:
                best = max(thumbs, key=lambda t: (t.get("height") or 0))
                thumb = best.get("url")
            title = e.get("title") or "—"
            for junk in (" (Official Music Video)", " (Official Video)", " [Official Video]",
                         " (Lyrics)", " [Lyrics]", " (Official Audio)", " [Official Audio]",
                         " (Audio)", " [Audio]", " (Lyric Video)", " [Lyric Video]"):
                title = title.replace(junk, "")
            tracks.append({
                "id": e.get("id"),
                "source": "yt",
                "title": title.strip(),
                "artists": e.get("channel") or e.get("uploader") or "",
                "album": "",
                "album_id": None,
                "cover": thumb,
                "duration_ms": int((duration or 0) * 1000),
                "liked": False,
            })
        result = {"tracks": tracks[:15]}
        _search_cache[text] = (result, time.time())
        return {"ok": True, **result}

    # ---------------------------- Стриминг ----------------------------

    def yt_stream(self, video_id: str, quality: str = "aac") -> dict:
        """Прямая аудиоссылка YouTube-видео (с кэшем — мгновенный запуск)."""
        cache_key = f"{video_id}:{quality}"
        cached = _stream_cache.get(cache_key)
        if cached and time.time() - cached[1] < _STREAM_TTL:
            return {"ok": True, **cached[0], "cached": True}
        if cached and time.time() - cached[1] < _STREAM_TTL * 2:
            return {"ok": True, **cached[0], "cached": True}

        fmt = {
            "opus": "bestaudio[acodec^=opus]/bestaudio",
            "aac": "bestaudio[acodec^=mp4a]/bestaudio",
        }.get(quality, "bestaudio")
        opts = dict(YDL_BASE)
        opts['format'] = fmt
        url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if info is None:
            return {"ok": False, "error": "no stream"}
        stream = info.get("url")
        if not stream:
            fmts = info.get("formats") or []
            audio = [f for f in fmts if f.get("acodec") != "none" and f.get("url")]
            if not audio:
                return {"ok": False, "error": "no audio formats"}
            best = max(audio, key=lambda f: (f.get("abr") or 0))
            stream = best["url"]
        result = {
            "bitrate": int(info.get("abr") or 0),
            "codec": info.get("ext", "m4a"),
            "title": info.get("title"),
        }
        # через локальный прокси — эквалайзер (Web Audio) сможет подключиться
        if PROXY_PORT:
            result["url"] = f"http://127.0.0.1:{PROXY_PORT}/{urllib.parse.quote(stream, safe='')}"
        else:
            result["url"] = stream
        _stream_cache[cache_key] = (result, time.time())
        return {"ok": True, **result}

    def open_url(self, url: str) -> None:
        webbrowser.open(url)

    # ---------------------------- Импорт плейлистов ----------------------------

    # состояние импорта для индикатора: {total, done, found, stage}
    _import_state = {"total": 0, "done": 0, "found": 0, "stage": "", "active": False}

    def import_progress(self) -> dict:
        """Прогресс текущего импорта — вызывается фронтендом раз в секунду."""
        return {"ok": True, **self._import_state}

    def import_playlist(self, url: str) -> dict:
        """Импорт плейлиста по ссылке: Яндекс Музыка или Spotify. Без лимита треков.

        Ищет каждый трек на YouTube параллельно; прогресс — в _import_state.
        """
        url = (url or "").strip()
        try:
            import requests
        except ImportError:
            return {"ok": False, "error": "requests не установлен"}

        titles = []
        pl_title = "Импортированный плейлист"
        self._import_state = {"total": 0, "done": 0, "found": 0, "stage": "чтение плейлиста", "active": True}

        # ---------- Яндекс Музыка ----------
        if "music.yandex" in url:
            # формат 1: users/LOGIN/playlists/KIND
            # формат 2: playlists/lk.UUID (новые короткие ссылки)
            m = re.search(r"users/([^/]+)/playlists/(\d+)", url)
            mu = re.search(r"playlists/(lk\.[a-f0-9-]+)", url)
            if not m and not mu:
                return {"ok": False, "error": "Ссылка Яндекса не распознана. Подойдёт music.yandex.ru/users/.../playlists/... или music.yandex.ru/playlists/lk...."}

            # 1) открываем страницу плейлиста и достаём flight-данные
            page_url = url if mu else f"https://music.yandex.ru/users/{m.group(1)}/playlists/{m.group(2)}"
            try:
                page = requests.get(
                    page_url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/146"},
                    timeout=15,
                )
                chunks = re.findall(r'self\.__next_f\.push\(\[1,\s*"((?:[^"\\]|\\.)*)"\]\)', page.text)
                blob = ""
                for ch in chunks:
                    try:
                        blob += json.loads('"' + ch + '"')
                    except Exception:
                        blob += ch
            except Exception as e:
                return {"ok": False, "error": f"Не удалось открыть страницу плейлиста: {e}"}

            kind = m.group(2) if m else None

            pl_title = None
            tracks_json = []
            owner_uid = None

            # 2) preloadedPlaylist / preloadedPlaylistByUuid в flight
            mp = re.search(r'"preloadedPlaylist(?:ByUuid)?":\s*(\{.*)', blob)
            if mp:
                s = mp.group(1)
                depth = 0
                for i, chch in enumerate(s):
                    if chch == "{":
                        depth += 1
                    elif chch == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                pl_data = json.loads(s[: i + 1])
                                pl_title = pl_data.get("title")
                                owner_uid = (pl_data.get("owner") or {}).get("uid")
                                if not kind:
                                    kind = str(pl_data.get("kind")) if pl_data.get("kind") else None
                            except Exception:
                                pass
                            break

            # 3) треки из официального API по uid
            if not owner_uid:
                mu = re.search(r'"owner":\{"uid":(\d+)', blob)
                if mu:
                    owner_uid = mu.group(1)
            if owner_uid:
                try:
                    resp = requests.get(
                        f"https://api.music.yandex.net/users/{owner_uid}/playlists/{kind}",
                        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                        timeout=15,
                    )
                    data = resp.json().get("result") or {}
                    pl_title = pl_title or data.get("title")
                    tracks_json = data.get("tracks") or []
                except Exception:
                    pass

            if not tracks_json:
                # фолбэк: названия треков прямо из flight
                for m3 in re.finditer(r'"title":"((?:[^"\\]|\\.)+)"[^{}]{0,200}?"type":"track"', blob):
                    titles.append((m3.group(1).encode().decode("unicode_escape", "replace"), None))

            for t in tracks_json:
                tt = (t or {}).get("track") or t or {}
                name = tt.get("title")
                artists = ", ".join(a.get("name") for a in (tt.get("artists") or []) if a.get("name"))
                if name:
                    # два варианта запроса: с артистом и без (уходим от строгого)
                    titles.append((f"{artists} {name}".strip(), None))
                    if artists:
                        titles.append((name.strip(), None))

        # ---------- Spotify ----------
        elif "spotify.com" in url or "open.spotify" in url:
            m = re.search(r"playlist[/:]([A-Za-z0-9]+)", url)
            if not m:
                return {"ok": False, "error": "Ссылка Spotify не распознана (нужна playlist/...)"}
            sp_id = m.group(1)
            embed_url = f"https://open.spotify.com/embed/playlist/{sp_id}"
            try:
                resp = requests.get(embed_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/146"}, timeout=15)
                html = resp.text
            except Exception as e:
                return {"ok": False, "error": f"Не удалось открыть плейлист Spotify: {e}"}
            mn = re.search(r'<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)</script>', html)
            if mn:
                try:
                    d = json.loads(mn.group(1))
                    entity = (d.get("props", {}).get("pageProps", {})
                              .get("state", {}).get("data", {}).get("entity") or {})
                    pl_title = entity.get("title") or pl_title
                    for tr in (entity.get("trackList") or []):
                        name = tr.get("title")
                        sub = tr.get("subtitle") or ""
                        if name:
                            titles.append((f"{sub} {name}".strip(), None))
                except Exception:
                    pass
        else:
            return {"ok": False, "error": "Не похоже на ссылку Яндекс Музыки или Spotify"}

        if not titles:
            self._import_state["active"] = False
            return {"ok": False, "error": "Плейлист пуст или недоступен (он должен быть публичным)"}

        # ---------- ищем ВСЕ треки на YouTube (параллельно, по 8) ----------
        found = []
        notfound = []
        self._import_state.update({"total": len(titles), "done": 0, "found": 0, "stage": "поиск треков на YouTube"})

        def look(q):
            r = self.yt_search(q)
            if r.get("ok") and r.get("tracks"):
                return r["tracks"][0]
            return None

        lock = __import__("threading").Lock()

        def look_counted(q):
            track = look(q)
            if track is None:
                # запасной запрос: убираем лишние слова, пробуем ещё раз
                alt = q.split(" (")[0].split(" feat")[0].strip()
                if alt and alt != q:
                    track = look(alt)
            with lock:
                self._import_state["done"] += 1
                if track:
                    self._import_state["found"] += 1
            return track

        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(look_counted, [q for q, _ in titles]))
        for (q, _), track in zip(titles, results):
            if track:
                found.append(track)
            else:
                notfound.append(q)

        # сохраняем как плейлист
        self._import_state.update({"stage": "сохранение", "done": len(titles)})
        d = _load()
        pl = {
            "id": str(int(time.time() * 1000)),
            "title": (pl_title or "Импорт") + " (импорт)",
            "tracks": [_clean_track(t) for t in found],
        }
        d.setdefault("playlists", []).append(pl)
        _save(d)
        self._import_state.update({"stage": "готово", "active": False})

        return {
            "ok": True,
            "title": pl["title"],
            "pid": pl["id"],
            "found": len(found),
            "notfound": len(notfound),
            "notfound_list": notfound[:10],
        }

    def wave_from_playlist(self, pid: str, played_json: str) -> dict:
        """Волна на основе треков конкретного плейлиста: похоже на каждый трек."""
        try:
            played = set(json.loads(played_json))
        except (ValueError, TypeError):
            played = set()
        d = _load()
        pl = next((p for p in d.get("playlists", []) if str(p.get("id")) == str(pid)), None)
        if not pl:
            return {"ok": False, "error": "плейлист не найден"}

        tracks = pl.get("tracks") or []
        played |= {t.get("id") for t in tracks}

        # затравки: артисты из треков + сами названия
        seeds = []
        for t in tracks:
            a = (t.get("artists") or "").strip()
            if a:
                seeds.append(a)
        seeds = list(dict.fromkeys(seeds))
        if len(seeds) > 8:
            seeds = random.sample(seeds, 8)
        # добавляем пару названий треков
        for t in random.sample(tracks, min(3, len(tracks))):
            if t.get("title"):
                seeds.append(t["title"] + " similar songs")
        queries = [s + random.choice(["", " mix", " playlist", " best songs"]) for s in seeds[:10]]

        tracks_out = {}
        with ThreadPoolExecutor(max_workers=6) as ex:
            results = list(ex.map(self.yt_search, queries))
        for r in results:
            if not r.get("ok"):
                continue
            for t in r.get("tracks", []):
                tid = t.get("id")
                if tid and tid not in played and tid not in tracks_out:
                    channel = t.get("artists")
                    if sum(1 for x in tracks_out.values() if x.get("artists") == channel) < 2:
                        tracks_out[tid] = t
        wave = list(tracks_out.values())
        random.shuffle(wave)
        return {"ok": True, "tracks": wave[:16]}

    def artist_tracks(self, artist: str) -> dict:
        """Топ-треки исполнителя на YouTube (с фолбэком и паузами при 403)."""
        queries = [artist, artist + " best songs", artist + " top songs"]
        tracks = {}
        for q in queries:
            r = self.yt_search(q)
            if not r.get("ok") and "403" in str(r.get("error", "")):
                time.sleep(3)  # YouTube попросил передышку — ждём и пробуем снова
                r = self.yt_search(q + " topic")
            if not r.get("ok"):
                continue
            for t in r.get("tracks", []):
                tid = t.get("id")
                if tid and tid not in tracks:
                    tracks[tid] = t
            if len(tracks) >= 15:
                break
        # сортируем: сначала с релевантного канала (в названии есть имя артиста)
        art = artist.lower()
        def score(t):
            ch = (t.get("artists") or "").lower()
            ti = (t.get("title") or "").lower()
            s = 0
            if art in ch: s += 3
            if art in ti: s += 2
            return -s
        out = sorted(tracks.values(), key=score)
        # убираем дубликаты по названию
        seen = set()
        dedup = []
        for t in out:
            key = (t.get("title") or "").lower().strip()
            if key not in seen:
                seen.add(key)
                dedup.append(t)
        return {"ok": True, "tracks": dedup[:20], "artist": artist}

    def subscribe_toggle(self, artist: str) -> dict:
        """Подписаться/отписаться на исполнителя."""
        d = _load()
        subs = d.setdefault("subscriptions", [])
        if artist in subs:
            subs.remove(artist)
            res = False
        else:
            subs.insert(0, artist)
            res = True
        _save(d)
        return {"ok": True, "subscribed": res}

    def get_subscriptions(self) -> list:
        d = _load()
        return d.get("subscriptions", [])

    def toggle_fullscreen(self) -> dict:
        """Нативный полный экран окна pywebview."""
        try:
            if webview.windows:
                webview.windows[0].toggle_fullscreen()
                return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": False, "error": "no window"}

    # ---------------------------- Текст песни ----------------------------

    def get_lyrics(self, video_id: str) -> dict:
        """Текст песни: субтитры YouTube (ru, потом en). Кэшируется навсегда (текст не меняется)."""
        if video_id in _lyrics_cache:
            return {"ok": True, **_lyrics_cache[video_id]}
        url = f"https://www.youtube.com/watch?v={video_id}"
        import tempfile, shutil
        tmp = tempfile.mkdtemp(prefix="ymp_sub_")
        try:
            for lang in ("ru", "en"):
                opts = dict(YDL_BASE)
                opts.update({
                    'skip_download': True,
                    'writesubtitles': True,
                    'writeautomaticsub': True,
                    'subtitleslangs': [lang],
                    'subtitlesformat': 'json3',
                    'outtmpl': os.path.join(tmp, 'sub.%(ext)s'),
                })
                try:
                    with YoutubeDL(opts) as ydl:
                        ydl.download([url])
                except Exception:
                    continue
                # ищем скачанный файл
                for f in os.listdir(tmp):
                    if f.endswith('.json3'):
                        with open(os.path.join(tmp, f), encoding="utf-8") as fp:
                            data = json.load(fp)
                        lines = []
                        for ev in data.get("events") or []:
                            segs = ev.get("segs")
                            if not segs:
                                continue
                            text = "".join(s.get("utf8", "") for s in segs).strip()
                            if not text or text == "\n":
                                continue
                            # убираем служебные вставки субтитров
                            text = re.sub(r"\[музыка\]|\[Music\]|\[Applause\]|\[аплодисменты\]", "", text).strip()
                            if not text:
                                continue
                            lines.append({"t": (ev.get("tStartMs") or 0) / 1000.0, "text": text})
                        # дедупликация подряд идущих повторов
                        dedup = []
                        for ln in lines:
                            if dedup and dedup[-1]["text"] == ln["text"]:
                                continue
                            dedup.append(ln)
                        if dedup:
                            _lyrics_cache[video_id] = {"lines": dedup, "lang": lang}
                            return {"ok": True, **_lyrics_cache[video_id]}
                        _lyrics_cache[video_id] = {"lines": [], "lang": lang}
                        return {"ok": True, **_lyrics_cache[video_id]}
            return {"ok": True, "lines": []}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ---------------------------- Библиотека ----------------------------

    def get_library(self) -> dict:
        d = _load()
        return {
            "ok": True,
            "liked": d.get("liked", []),
            "playlists": d.get("playlists", []),
            "history": d.get("history", []),
        }

    def add_history(self, q: str) -> dict:
        d = _load()
        h = d.setdefault("history", [])
        if q in h:
            h.remove(q)
        h.insert(0, q)
        del h[15:]
        _save(d)
        return {"ok": True}

    def toggle_like(self, track: dict) -> dict:
        d = _load()
        t = _clean_track(track)
        liked = d.get("liked", [])
        if t["id"] in {x.get("id") for x in liked}:
            d["liked"] = [x for x in liked if x.get("id") != t["id"]]
            res = False
        else:
            liked.insert(0, t)
            d["liked"] = liked
            res = True
        _save(d)
        return {"ok": True, "liked": res}

    def create_playlist(self, title: str) -> dict:
        d = _load()
        pl = {"id": str(int(time.time() * 1000)), "title": title.strip() or "Новый плейлист", "tracks": []}
        d.setdefault("playlists", []).append(pl)
        _save(d)
        return {"ok": True, "playlist": pl}

    def delete_playlist(self, pid: str) -> dict:
        d = _load()
        d["playlists"] = [p for p in d.get("playlists", []) if str(p.get("id")) != str(pid)]
        _save(d)
        return {"ok": True}

    def add_to_playlist(self, pid: str, track: dict) -> dict:
        d = _load()
        for p in d.get("playlists", []):
            if str(p.get("id")) == str(pid):
                t = _clean_track(track)
                if t["id"] not in {x.get("id") for x in p.get("tracks", [])}:
                    p.setdefault("tracks", []).insert(0, t)
                    _save(d)
                    return {"ok": True, "added": True}
                return {"ok": True, "added": False}
        return {"ok": False, "error": "playlist not found"}

    def remove_from_playlist(self, pid: str, index: int) -> dict:
        d = _load()
        for p in d.get("playlists", []):
            if str(p.get("id")) == str(pid):
                tracks = p.get("tracks", [])
                if 0 <= index < len(tracks):
                    tracks.pop(index)
                    _save(d)
                return {"ok": True}
        return {"ok": False, "error": "playlist not found"}

    # ---------------------------- Моя волна ----------------------------

    def get_wave_by_genre(self, genre_query: str, played_json: str) -> dict:
        """Волна в конкретном жанре: несколько вариаций запроса вокруг жанра."""
        try:
            played = set(json.loads(played_json))
        except (ValueError, TypeError):
            played = set()
        queries = [
            genre_query,
            genre_query + " mix",
            genre_query + " playlist 2024",
            genre_query + " best",
        ]
        random.shuffle(queries)
        tracks = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(self.yt_search, queries))
        for r in results:
            if not r.get("ok"):
                continue
            for t in r.get("tracks", []):
                tid = t.get("id")
                if tid and tid not in played and tid not in tracks:
                    channel = t.get("artists")
                    if sum(1 for x in tracks.values() if x.get("artists") == channel) < 2:
                        tracks[tid] = t
        wave = list(tracks.values())
        random.shuffle(wave)
        return {"ok": True, "tracks": wave[:16]}

    def get_wave(self, played_json: str) -> dict:
        """Персональный бесконечный поток.

        Затравки: артисты из лайков + история поиска + сами лайкнутые треки.
        Половина запросов — «похожее на артиста», половина — сами лайки.
        """
        try:
            played = set(json.loads(played_json))
        except (ValueError, TypeError):
            played = set()

        d = _load()
        liked = d.get("liked", [])
        liked_ids = {t.get("id") for t in liked}
        played |= liked_ids  # лайкнутое уже есть в «Мне нравится» — не повторяем

        # 1) артисты из лайков
        artists = []
        for t in liked:
            a = (t.get("artists") or "").strip()
            if a and a not in ("Various Artists", "-"):
                artists.append(a)
        # 2) названия лайкнутых треков (для «похожих» запросов)
        liked_titles = [(t.get("title") or "").strip() for t in liked if t.get("title")]
        # 3) история поиска
        history = [q.strip() for q in d.get("history", []) if q]

        artists = list(dict.fromkeys(artists))
        if len(artists) > 6:
            artists = random.sample(artists, 6)

        queries = []
        # похожее на любимых артистов
        for a in artists[:4]:
            queries.append(a + random.choice([" best songs", "", " playlist", " top songs"]))
        # похожее на лайкнутые треки
        for title in random.sample(liked_titles, min(2, len(liked_titles))):
            queries.append(title + " similar songs")
        # из истории
        for q in history[:3]:
            queries.append(q)
        if not queries:
            queries = [
                "popular hits", "top songs this week", "best rock songs", "зарубежные хиты",
                random.choice(["lofi beats", "chill mix", "top charts", "80s hits", "90s hits"]),
            ]
        random.shuffle(queries)
        queries = queries[:6]

        tracks = {}
        with ThreadPoolExecutor(max_workers=min(6, len(queries))) as ex:
            results = list(ex.map(self.yt_search, queries))
        for r in results:
            if not r.get("ok"):
                continue
            for t in r.get("tracks", []):
                tid = t.get("id")
                if tid and tid not in played and tid not in tracks:
                    # не больше 2 треков с одного канала — разнообразие
                    channel = t.get("artists")
                    channel_count = sum(1 for x in tracks.values() if x.get("artists") == channel)
                    if channel_count < 2:
                        tracks[tid] = t

        wave = list(tracks.values())
        # перемешиваем, но лайки не дублируем
        random.shuffle(wave)
        return {"ok": True, "tracks": wave[:16]}


api = Api()


# ---------------------------------------------------------------------------
# Локальный стрим-прокси: добавляет CORS-заголовки, чтобы работал эквалайзер
# (Web Audio требует same-origin / CORS у источника аудио)
# ---------------------------------------------------------------------------

class _StreamProxy(BaseHTTPRequestHandler):
    quiet = True

    def log_message(self, *args):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Range")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        import urllib.request
        target = urllib.parse.unquote(self.path.lstrip("/"))
        if not target.startswith("http"):
            self.send_response(400)
            self._cors()
            self.end_headers()
            return
        rng = self.headers.get("Range")
        req_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        }
        if rng:
            req_headers["Range"] = rng
        try:
            req = urllib.request.Request(target, headers=req_headers)
            with urllib.request.urlopen(req, timeout=30) as upstream:
                status = upstream.status
                self.send_response(status)
                self._cors()
                for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                    if h in upstream.headers:
                        self.send_header(h, upstream.headers[h])
                self.end_headers()
                while True:
                    chunk = upstream.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except Exception:
            try:
                self.send_response(502)
                self._cors()
                self.end_headers()
            except Exception:
                pass


def start_proxy() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StreamProxy)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return port


PROXY_PORT = None  # заполняется в main()


def main() -> None:
    global PROXY_PORT
    PROXY_PORT = start_proxy()
    html_path = os.path.join(BASE_DIR, "ui.html")
    # AppUserModelID — чтобы иконка exe корректно показывалась в панели задач
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ymp.player.1")
    except Exception:
        pass
    webview.create_window(
        "Sponki",
        url=html_path,
        js_api=api,
        width=1150,
        height=780,
        min_size=(860, 600),
    )
    webview.start()


if __name__ == "__main__":
    main()
