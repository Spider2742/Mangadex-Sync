#!/usr/bin/env python3
"""
MangaDex All-in-One Exporter — Web Edition
Run: python mangadex_web.py
Then open: http://localhost:7337
"""

import threading, time, json, os, re, gzip, queue, webbrowser
from datetime import datetime, timedelta
from collections import defaultdict
import requests as req_lib
import pandas as pd
from flask import Flask, Response, jsonify, request, send_from_directory

# ── Config ─────────────────────────────────────────────────────────────────────
PORT      = 7337
API_BASE  = "https://api.mangadex.org"
AUTH_URL  = "https://auth.mangadex.org/realms/mangadex/protocol/openid-connect/token"
MAX_RETRY = 3
STATUSES  = ["reading","completed","on_hold","dropped","plan_to_read","re_reading"]
MAL_MAP   = {"reading":"Reading","completed":"Completed","on_hold":"On-Hold",
             "dropped":"Dropped","plan_to_read":"Plan to Read","re_reading":"Reading"}
MAL_REVERSE = {"Reading":"reading","Completed":"completed","On-Hold":"on_hold",
               "Dropped":"dropped","Plan to Read":"plan_to_read"}

app = Flask(__name__)

# ── Global state ───────────────────────────────────────────────────────────────
_state = dict(
    running=False, progress=0, label="Ready", eta="",
    log_queue=queue.Queue(), stop=threading.Event(),
    api=None, exported=[], skipped=[],
)
_history_file = "mdex_history.json"
_checkpoint_file = "mdex_checkpoint.json"

# ── MangaDex API ───────────────────────────────────────────────────────────────
class API:
    def __init__(self):
        self.session = req_lib.Session()
        self.access_token = self.refresh_token = None
        self.client_id = self.client_secret = None
        self.expires_at = None

    def auth(self, cid, csec, user, pwd):
        self.client_id, self.client_secret = cid, csec
        payload = dict(grant_type="password", username=user, password=pwd,
                       client_id=cid, client_secret=csec)
        for _ in range(MAX_RETRY):
            try:
                r = self.session.post(AUTH_URL, data=payload, timeout=20)
                if r.status_code == 200:
                    self._store(r.json()); return True
                return False
            except Exception: time.sleep(2)
        return False

    def _store(self, d):
        self.access_token = d["access_token"]
        self.refresh_token = d.get("refresh_token")
        self.expires_at = datetime.now() + timedelta(seconds=d.get("expires_in",900)-60)
        self.session.headers["Authorization"] = f"Bearer {self.access_token}"

    def _ensure(self):
        if self.expires_at and datetime.now() >= self.expires_at:
            try:
                r = self.session.post(AUTH_URL, timeout=20, data=dict(
                    grant_type="refresh_token", refresh_token=self.refresh_token,
                    client_id=self.client_id, client_secret=self.client_secret))
                if r.status_code == 200: self._store(r.json())
            except Exception: pass

    def get(self, url, params=None):
        self._ensure()
        for attempt in range(MAX_RETRY):
            try:
                r = self.session.get(url, params=params, timeout=30)
                if r.status_code == 200: return r.json()
                if r.status_code == 429:
                    time.sleep(int(r.headers.get("Retry-After",5)))
                elif attempt < MAX_RETRY-1: time.sleep(2)
            except Exception:
                if attempt < MAX_RETRY-1: time.sleep(2)
        return None

    def statuses(self):
        d = self.get(f"{API_BASE}/manga/status")
        if not d: return {}
        out = defaultdict(list)
        for mid, st in d.get("statuses",{}).items(): out[st].append(mid)
        return dict(out)

    def manga_details(self, ids, cb=None):
        out, bs = {}, 100
        for i, batch in enumerate([ids[j:j+bs] for j in range(0,len(ids),bs)]):
            d = self.get(f"{API_BASE}/manga", {"ids[]":batch,"limit":100,"includes[]":["author"]})
            if d:
                for m in d.get("data",[]): out[m["id"]] = m
            if cb: cb(min((i+1)*bs,len(ids)), len(ids))
            time.sleep(0.25)
        return out

    def read_chapters(self, ids):
        out, bs = {}, 100
        for batch in [ids[i:i+bs] for i in range(0,len(ids),bs)]:
            d = self.get(f"{API_BASE}/manga/read", {"ids[]":batch,"grouped":"true"})
            if d: out.update(d.get("data",{}))
            time.sleep(0.25)
        return out

    def chapter_details(self, ids, cb=None):
        out, bs = {}, 100
        for i, batch in enumerate([ids[j:j+bs] for j in range(0,len(ids),bs)]):
            d = self.get(f"{API_BASE}/chapter", {"ids[]":batch,"limit":100})
            if d:
                for ch in d.get("data",[]): out[ch["id"]] = ch
            if cb: cb(min((i+1)*bs,len(ids)), len(ids))
            time.sleep(0.25)
        return out

    def ratings(self, ids):
        out, bs = {}, 100
        for batch in [ids[i:i+bs] for i in range(0,len(ids),bs)]:
            d = self.get(f"{API_BASE}/rating", {"manga[]":batch})
            if d:
                for mid, rd in d.get("ratings",{}).items():
                    out[mid] = rd.get("rating",0)
            time.sleep(0.25)
        return out

    def put(self, url, body):
        self._ensure()
        for attempt in range(MAX_RETRY):
            try:
                r = self.session.put(url, json=body, timeout=20)
                if r.status_code in (200, 204): return True
                if r.status_code == 429:
                    time.sleep(int(r.headers.get("Retry-After", 5)))
                elif attempt < MAX_RETRY - 1: time.sleep(2)
            except Exception:
                if attempt < MAX_RETRY - 1: time.sleep(2)
        return False

    def post_json(self, url, body):
        self._ensure()
        for attempt in range(MAX_RETRY):
            try:
                r = self.session.post(url, json=body, timeout=20)
                if r.status_code in (200, 201, 204): return True
                if r.status_code == 429:
                    time.sleep(int(r.headers.get("Retry-After", 5)))
                elif attempt < MAX_RETRY - 1: time.sleep(2)
            except Exception:
                if attempt < MAX_RETRY - 1: time.sleep(2)
        return False

    def delete(self, url):
        self._ensure()
        for attempt in range(MAX_RETRY):
            try:
                r = self.session.delete(url, timeout=20)
                if r.status_code in (200, 204): return True
                if r.status_code == 429:
                    time.sleep(int(r.headers.get("Retry-After", 5)))
                elif attempt < MAX_RETRY - 1: time.sleep(2)
            except Exception:
                if attempt < MAX_RETRY - 1: time.sleep(2)
        return False

    def set_status(self, manga_id, status):
        """Set reading status for a manga. Pass None to remove.
        Returns (True, "") on success, or (False, "reason") on failure."""
        if status is None:
            ok = self.delete(f"{API_BASE}/manga/{manga_id}/status")
            return (True, "") if ok else (False, "delete failed")
        self._ensure()
        url = f"{API_BASE}/manga/{manga_id}/status"
        last_err = "unknown error"
        for attempt in range(MAX_RETRY):
            try:
                r = self.session.post(url, json={"status": status}, timeout=20)
                if r.status_code in (200, 201, 204):
                    return (True, "")
                if r.status_code == 429:
                    time.sleep(int(r.headers.get("Retry-After", 5)))
                    continue
                try:
                    err_body = r.json()
                    detail = err_body.get("errors", [{}])[0].get("detail", "")
                    last_err = f"HTTP {r.status_code}: {detail or r.text[:100]}"
                except Exception:
                    last_err = f"HTTP {r.status_code}: {r.text[:100]}"
                if attempt < MAX_RETRY - 1:
                    time.sleep(2)
            except Exception as e:
                last_err = f"network error: {e}"
                if attempt < MAX_RETRY - 1:
                    time.sleep(2)
        return (False, last_err)

    def set_rating(self, manga_id, rating):
        """Set rating (1-10) for a manga. Pass 0 to skip."""
        if not rating or rating <= 0: return True
        return self.post_json(f"{API_BASE}/rating/{manga_id}", {"rating": int(rating)})

    def find_by_mal_id(self, mal_id):
        """Search MangaDex for a manga by its MAL ID. Returns MangaDex UUID or None."""
        d = self.get(f"{API_BASE}/manga", {"links[mal]": str(mal_id), "limit": 1})
        if d and d.get("data"):
            return d["data"][0]["id"]
        return None

# ── Helpers ────────────────────────────────────────────────────────────────────
def _log(msg, tag="info"):
    ts = datetime.now().strftime("%H:%M:%S")
    _state["log_queue"].put(json.dumps({"ts":ts,"msg":msg,"tag":tag}))

def _prog(pct, label="", eta=""):
    _state["progress"] = pct
    _state["label"] = label
    _state["eta"] = eta

def _save_history(entry):
    log = []
    if os.path.exists(_history_file):
        try:
            with open(_history_file) as f: log = json.load(f)
        except Exception: pass
    log.insert(0, entry)
    with open(_history_file,"w") as f: json.dump(log[:100], f, indent=2)

def _save_checkpoint(data):
    with open(_checkpoint_file,"w") as f: json.dump(data, f, indent=2)

def _load_checkpoint():
    if os.path.exists(_checkpoint_file):
        try:
            with open(_checkpoint_file) as f: return json.load(f)
        except Exception: pass
    return None

def _clear_checkpoint():
    try: os.remove(_checkpoint_file)
    except Exception: pass

def _write_xml(entries, path, uid, uname, gz=False):
    with_id = [e for e in entries if e.get("mal_id")]
    counts = defaultdict(int)
    for e in with_id: counts[e["mal_status"]] += 1
    lines = [
        '<?xml version="1.0" encoding="UTF-8" ?>',
        '<myanimelist>','<myinfo>',
        f'  <user_id>{uid}</user_id>',
        f'  <user_name>{uname}</user_name>',
        '  <user_export_type>2</user_export_type>',
        f'  <user_total_manga>{len(with_id)}</user_total_manga>',
        f'  <user_total_reading>{counts["Reading"]}</user_total_reading>',
        f'  <user_total_completed>{counts["Completed"]}</user_total_completed>',
        f'  <user_total_onhold>{counts["On-Hold"]}</user_total_onhold>',
        f'  <user_total_dropped>{counts["Dropped"]}</user_total_dropped>',
        f'  <user_total_plantoread>{counts["Plan to Read"]}</user_total_plantoread>',
        '</myinfo>',
    ]
    for e in with_id:
        times = "1" if e["mal_status"]=="Completed" else "0"
        lines += ['<manga>',
            f'  <manga_mangadb_id>{e["mal_id"]}</manga_mangadb_id>',
            f'  <manga_title><![CDATA[{e["title"]}]]></manga_title>',
            f'  <my_read_volumes>{e.get("volume",0)}</my_read_volumes>',
            f'  <my_read_chapters>{e.get("chapter",0)}</my_read_chapters>',
            '  <my_start_date>0000-00-00</my_start_date>',
            '  <my_finish_date>0000-00-00</my_finish_date>',
            f'  <my_score>{e.get("score",0)}</my_score>',
            f'  <my_status>{e["mal_status"]}</my_status>',
            f'  <my_times_read>{times}</my_times_read>',
            '  <my_tags><![CDATA[]]></my_tags>',
            '  <my_priority>Low</my_priority>',
            '  <update_on_import>1</update_on_import>',
            '</manga>']
    lines.append('</myanimelist>')
    content = '\n'.join(lines)
    with open(path,"w",encoding="utf-8") as f: f.write(content)
    if gz:
        with open(path,"rb") as fi, gzip.open(path+".gz","wb") as fo:
            fo.write(fi.read())

def _guess_status(path):
    n = os.path.basename(path).lower()
    if "re_reading" in n or "re-reading" in n: return "Reading"
    if "reading" in n: return "Reading"
    if "completed" in n: return "Completed"
    if "on_hold" in n or "on-hold" in n: return "On-Hold"
    if "dropped" in n: return "Dropped"
    if "plan" in n: return "Plan to Read"
    return "Reading"

# ── Export worker ──────────────────────────────────────────────────────────────
def _run_export(params, resume_cp=None):
    _state["running"] = True
    _state["stop"].clear()
    _state["skipped"] = []
    api = API()
    _state["api"] = api

    try:
        _log("Authenticating…", "info")
        ok = api.auth(params["client_id"], params["client_secret"],
                      params["username"], params["password"])
        if not ok:
            _log("Authentication failed. Check credentials.", "error")
            return

        _log("✓ Auth successful!", "success")
        _log("Fetching library statuses…", "info")

        all_st = api.statuses()
        if not all_st:
            _log("No manga found in your library.", "error"); return

        target = params.get("status")
        if target: all_st = {target: all_st.get(target,[])}

        done_list = resume_cp.get("completed",[]) if resume_cp else []
        total = sum(len(v) for v in all_st.values())
        _log(f"Found {total} manga across {len(all_st)} status group(s)")

        mode      = params.get("mode","fast")
        save_dir  = params.get("save_dir", os.getcwd())
        uid       = params.get("mal_user_id","")
        uname     = params.get("mal_username","user")
        dry_run   = params.get("dry_run", False)

        all_ids = [mid for ids in all_st.values() for mid in ids]
        _log("Fetching your ratings…", "info")
        try: ratings = api.ratings(all_ids)
        except Exception: ratings = {}
        _log(f"✓ {len(ratings)} ratings found")

        exported_files, start = [], datetime.now()

        for status, manga_ids in all_st.items():
            if _state["stop"].is_set():
                _log("Stopped by user.", "warning"); break
            if status in done_list:
                _log(f"Skipping '{status}' (already done)", "info"); continue
            if not manga_ids: continue

            _log(f"── {status.upper()} ({len(manga_ids)} manga) ──", "info")
            _prog(0, f"Processing {status}…")

            # Manga details
            t0 = datetime.now()
            def det_cb(done, total, _t0=t0):
                pct = done/total * (60 if mode=="deep" else 85)
                elapsed = (datetime.now()-_t0).total_seconds()
                eta = f"{int((elapsed/done)*(total-done)//60)}m {int((elapsed/done)*(total-done)%60)}s" if done else ""
                _prog(pct, f"Fetching details {done}/{total}", eta)

            details = api.manga_details(manga_ids, det_cb)
            _log(f"✓ {len(details)} manga details fetched")

            # Deep mode chapters
            read_map = {}
            if mode == "deep":
                _log("Fetching read chapter IDs…", "info")
                _prog(60, "Fetching read chapters…")
                ch_by_manga = api.read_chapters(manga_ids)
                all_ch = list({cid for ids in ch_by_manga.values() for cid in ids})
                _log(f"Fetching details for {len(all_ch)} chapters…")

                def ch_cb(done, total):
                    _prog(60+done/total*30, f"Chapters {done}/{total}")

                ch_det = api.chapter_details(all_ch, ch_cb)
                for mid, cids in ch_by_manga.items():
                    best_ch = best_vol = 0.0
                    for cid in cids:
                        attrs = ch_det.get(cid,{}).get("attributes",{})
                        try:
                            n = float(attrs.get("chapter") or 0)
                            v = float(attrs.get("volume") or 0)
                            if n > best_ch: best_ch, best_vol = n, v
                        except Exception: pass
                    read_map[mid] = (best_ch, best_vol)

            # Build entries
            entries, skipped = [], []
            for mid, manga in details.items():
                attrs  = manga.get("attributes",{})
                titles = attrs.get("title",{})
                title  = (titles.get("en") or titles.get("ja-ro")
                           or next(iter(titles.values()),"Unknown"))
                links  = attrs.get("links",{}) or {}
                mal_id = links.get("mal")
                ch, vol = read_map.get(mid,(0,0)) if mode=="deep" else (0,0)
                score  = ratings.get(mid,0)
                entries.append(dict(manga_id=mid, title=title, status=status,
                    mal_status=MAL_MAP.get(status,"Reading"), mal_id=mal_id,
                    anilist_id=links.get("al"), chapter=int(ch), volume=int(vol), score=score))
                if not mal_id: skipped.append(title)

            if skipped:
                _log(f"⚠ {len(skipped)} manga have no MAL ID", "warning")
                _state["skipped"].extend(skipped)

            if dry_run:
                _log(f"[DRY RUN] Would save {len(entries)} entries for '{status}'","warning")
                done_list.append(status)
                continue

            ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            out = []

            # XLSX
            xp = os.path.join(save_dir, f"mdex_{status}_{ts_str}.xlsx")
            pd.DataFrame(entries).to_excel(xp, index=False)
            out.append(xp); exported_files.append(xp)
            _log(f"✓ XLSX saved: {os.path.basename(xp)}", "success")

            # JSON
            if params.get("fmt_json"):
                jp = os.path.join(save_dir, f"mdex_{status}_{ts_str}.json")
                with open(jp,"w",encoding="utf-8") as jf:
                    json.dump(entries, jf, indent=2, ensure_ascii=False)
                out.append(jp); _log(f"✓ JSON saved: {os.path.basename(jp)}", "success")

            # MAL XML
            if params.get("fmt_mal"):
                mp = os.path.join(save_dir, f"mal_{status}_{ts_str}.xml")
                _write_xml(entries, mp, uid, uname, gz=True)
                out.append(mp); _log(f"✓ MAL XML saved: {os.path.basename(mp)}", "success")

            # AniList XML
            if params.get("fmt_al"):
                ap = os.path.join(save_dir, f"anilist_{status}_{ts_str}.xml")
                _write_xml(entries, ap, uid, uname, gz=False)
                out.append(ap); _log(f"✓ AniList XML saved: {os.path.basename(ap)}", "success")

            done_list.append(status)
            _save_checkpoint({"completed":done_list,"timestamp":datetime.now().isoformat()})
            _prog(100, f"✓ '{status}' done")

        if not _state["stop"].is_set():
            _clear_checkpoint()
            _state["exported"] = [f for f in exported_files if f.endswith(".xlsx")]
            elapsed = int((datetime.now()-start).total_seconds())
            _save_history({"date":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "type": target or "Full Library",
                "total": total, "mode": mode,
                "files": ", ".join(os.path.basename(f) for f in exported_files),
                "elapsed": f"{elapsed}s"})
            _log(f"✓ Export complete in {elapsed}s! {len(exported_files)} file(s) saved.", "success")

    except Exception as e:
        _log(f"Error: {e}", "error")
    finally:
        _state["running"] = False
        _prog(0, "Ready")

# ── Import worker ──────────────────────────────────────────────────────────────
def _parse_mal_xml(path):
    """Parse a MAL/AniList XML file. Returns list of dicts with mal_id, title, status, score."""
    import xml.etree.ElementTree as ET
    tree = ET.parse(path)
    root = tree.getroot()
    entries = []
    for manga in root.findall("manga"):
        def t(tag): v = manga.find(tag); return v.text.strip() if v is not None and v.text else ""
        mal_id  = t("manga_mangadb_id")
        title   = t("manga_title")
        status  = t("my_status")
        score   = t("my_score")
        chapter = t("my_read_chapters")
        volume  = t("my_read_volumes")
        if mal_id:
            entries.append(dict(
                mal_id=mal_id, title=title, status=status,
                score=int(score) if score.isdigit() else 0,
                chapter=int(chapter) if chapter.isdigit() else 0,
                volume=int(volume) if volume.isdigit() else 0,
            ))
    return entries

def _parse_json_backup(path):
    """Parse a mdex_*.json backup file. Returns list of dicts with manga_id, status, score."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def _run_import(params):
    _state["running"] = True
    _state["stop"].clear()
    _state["skipped"] = []

    try:
        _log("Authenticating…", "info")
        api = API()
        ok = api.auth(params["client_id"], params["client_secret"],
                      params["username"], params["password"])
        if not ok:
            _log("Authentication failed. Check credentials.", "error"); return
        _log("✓ Auth successful!", "success")

        file_path   = params["file_path"]
        file_type   = params["file_type"]   # "xml" or "json"
        import_scores = params.get("import_scores", True)
        dry_run     = params.get("dry_run", False)

        # ── Parse file ─────────────────────────────────────────────────────────
        _log(f"Parsing {os.path.basename(file_path)}…", "info")
        if file_type == "xml":
            raw = _parse_mal_xml(file_path)
            _log(f"✓ Found {len(raw)} entries in XML", "success")
        else:
            raw = _parse_json_backup(file_path)
            _log(f"✓ Found {len(raw)} entries in JSON backup", "success")

        total   = len(raw)
        ok_cnt  = 0
        skip_cnt = 0
        start   = datetime.now()

        for i, entry in enumerate(raw):
            if _state["stop"].is_set():
                _log("Stopped by user.", "warning"); break

            pct = (i / total) * 100
            _prog(pct, f"Importing {i+1}/{total}…")

            title = entry.get("title", "Unknown")

            # ── Resolve MangaDex UUID ──────────────────────────────────────────
            if file_type == "json":
                mdex_id = entry.get("manga_id")
                mdex_status = entry.get("status")
                score = entry.get("score", 0)
            else:
                # XML — need to look up MangaDex UUID from MAL ID
                mal_id = entry.get("mal_id")
                mal_status = entry.get("status", "Reading")
                score = entry.get("score", 0)
                mdex_status = MAL_REVERSE.get(mal_status, "reading")

                _log(f"Looking up '{title}' (MAL #{mal_id})…", "info")
                mdex_id = api.find_by_mal_id(mal_id)
                time.sleep(0.3)  # be nice to the API

            if not mdex_id:
                _log(f"⚠ Could not find MangaDex ID for '{title}' — skipped", "warning")
                _state["skipped"].append(title)
                skip_cnt += 1
                continue

            if dry_run:
                _log(f"[DRY RUN] Would set '{title}' → {mdex_status} (score: {score})", "info")
                ok_cnt += 1
                continue

            # ── Set status ─────────────────────────────────────────────────────
            status_ok, status_err = api.set_status(mdex_id, mdex_status)
            if not status_ok:
                _log(f"⚠ Failed to set status for '{title}' — {status_err}", "warning")
                _state["skipped"].append(title)
                skip_cnt += 1
                continue

            # ── Set rating ─────────────────────────────────────────────────────
            if import_scores and score and score > 0:
                api.set_rating(mdex_id, score)

            ok_cnt += 1
            if i % 10 == 0 or i == total - 1:
                _log(f"✓ {ok_cnt} imported, {skip_cnt} skipped so far…", "success")

            time.sleep(0.2)

        elapsed = int((datetime.now() - start).total_seconds())
        verb = "would be imported" if dry_run else "imported"
        _log(f"✓ Done in {elapsed}s! {ok_cnt} manga {verb}, {skip_cnt} skipped.", "success")
        _prog(100, "Import complete!")

        _save_history({"date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "type": f"Import ({file_type.upper()})",
                       "total": ok_cnt, "skipped": skip_cnt,
                       "mode": "dry-run" if dry_run else "import",
                       "elapsed": f"{elapsed}s",
                       "files": os.path.basename(file_path)})

    except Exception as e:
        _log(f"Error: {e}", "error")
    finally:
        _state["running"] = False
        _prog(0, "Ready")

# ── Flask routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return HTML_PAGE

@app.route("/api/stream")
def stream():
    def gen():
        while True:
            try:
                msg = _state["log_queue"].get(timeout=0.5)
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'ping':1})}\n\n"
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.route("/api/status")
def status():
    cp = _load_checkpoint()
    return jsonify(running=_state["running"],
                   progress=_state["progress"],
                   label=_state["label"],
                   eta=_state["eta"],
                   exported=_state["exported"],
                   skipped=_state["skipped"],
                   has_checkpoint=cp is not None,
                   checkpoint_done=cp.get("completed",[]) if cp else [])

@app.route("/api/export", methods=["POST"])
def export():
    if _state["running"]:
        return jsonify(ok=False, error="Already running"), 400
    params = request.json
    threading.Thread(target=_run_export, args=(params,), daemon=True).start()
    return jsonify(ok=True)

@app.route("/api/resume", methods=["POST"])
def resume():
    if _state["running"]:
        return jsonify(ok=False, error="Already running"), 400
    cp = _load_checkpoint()
    if not cp:
        return jsonify(ok=False, error="No checkpoint found"), 404
    params = request.json
    threading.Thread(target=_run_export, args=(params, cp), daemon=True).start()
    return jsonify(ok=True)

@app.route("/api/stop", methods=["POST"])
def stop():
    _state["stop"].set()
    return jsonify(ok=True)

@app.route("/api/history")
def history():
    if os.path.exists(_history_file):
        try:
            with open(_history_file) as f: return jsonify(json.load(f))
        except Exception: pass
    return jsonify([])

@app.route("/api/checkpoint")
def checkpoint():
    cp = _load_checkpoint()
    return jsonify(cp or {})

@app.route("/api/checkpoint/clear", methods=["POST"])
def clear_checkpoint():
    _clear_checkpoint()
    return jsonify(ok=True)

@app.route("/api/convert", methods=["POST"])
def convert():
    data = request.json
    uid   = data.get("mal_user_id","")
    uname = data.get("mal_username","user")
    files = data.get("files",[])
    dry   = data.get("dry_run", False)
    incl_scores = data.get("include_scores", True)
    fmt_mal = data.get("fmt_mal", True)
    fmt_al  = data.get("fmt_al", True)
    save_dir = data.get("save_dir", os.getcwd())

    if not files:
        return jsonify(ok=False, error="No files provided"), 400

    entries, skipped = [], []
    for fi in files:
        path   = fi.get("path","")
        status = fi.get("status","Reading")
        if not os.path.exists(path):
            return jsonify(ok=False, error=f"File not found: {path}"), 404
        try: df = pd.read_excel(path)
        except Exception as e:
            return jsonify(ok=False, error=f"Could not read {path}: {e}"), 400

        for _, row in df.iterrows():
            raw_mal = row.get("mal_id") or row.get("myanimelist")
            title   = str(row.get("title","Unknown"))
            ch      = row.get("chapter",0) or row.get("latestReadChapter",0) or 0
            vol     = row.get("volume",0)  or row.get("latestReadVolume",0)  or 0
            score   = (row.get("score",0) if incl_scores else 0)

            def _i(v):
                try: return int(float(v)) if v and str(v)!="nan" else 0
                except Exception: return 0

            if pd.isna(raw_mal) if hasattr(pd,"isna") else (raw_mal != raw_mal):
                skipped.append(title); continue
            if not str(raw_mal).strip():
                skipped.append(title); continue
            m = re.search(r"(\d+)", str(raw_mal))
            if not m: skipped.append(title); continue
            entries.append(dict(mal_id=m.group(1), title=title, mal_status=status,
                                chapter=_i(ch), volume=_i(vol), score=_i(score)))

    if dry:
        return jsonify(ok=True, dry=True, total=len(entries), skipped=len(skipped),
                       skipped_titles=skipped[:50])

    if not entries:
        return jsonify(ok=False, error="No valid entries (all missing MAL IDs)"), 400

    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    saved = []
    if fmt_mal:
        p = os.path.join(save_dir, f"mal_import_{ts_str}.xml")
        _write_xml(entries, p, uid, uname, gz=True)
        saved.append(p)
    if fmt_al:
        p = os.path.join(save_dir, f"anilist_import_{ts_str}.xml")
        _write_xml(entries, p, uid, uname, gz=False)
        saved.append(p)

    _save_history({"date":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "type":"Convert","total":len(entries),"skipped":len(skipped),
                   "mode":"convert","files":", ".join(os.path.basename(s) for s in saved)})

    return jsonify(ok=True, total=len(entries), skipped=len(skipped),
                   skipped_titles=skipped[:50], files=saved)

@app.route("/api/exported_files")
def exported_files():
    return jsonify(_state["exported"])

@app.route("/api/import", methods=["POST"])
def do_import():
    if _state["running"]:
        return jsonify(ok=False, error="Already running"), 400
    params = request.json
    if not params.get("file_path"):
        return jsonify(ok=False, error="No file path provided"), 400
    if not os.path.exists(params["file_path"]):
        return jsonify(ok=False, error=f"File not found: {params['file_path']}"), 404
    # Auto-detect file type from extension if not provided
    if not params.get("file_type"):
        ext = os.path.splitext(params["file_path"])[1].lower()
        params["file_type"] = "json" if ext == ".json" else "xml"
    threading.Thread(target=_run_import, args=(params,), daemon=True).start()
    return jsonify(ok=True)

def _run_tk_subprocess(script, timeout=60):
    """Run a tkinter snippet in a subprocess to avoid Qt/tkinter thread conflicts.

    Flask routes run on worker threads; tkinter (and Qt via pywebview) both
    require the main thread.  Spawning a child process gives tkinter its own
    main thread, completely isolated from the Qt event loop.
    Returns the stripped stdout of the script, or "" on any error.
    """
    import subprocess, sys
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip()
    except Exception:
        return ""

_TK_BROWSE_FOLDER = """
import tkinter as tk
from tkinter import filedialog
root = tk.Tk(); root.withdraw(); root.wm_attributes('-topmost', True)
print(filedialog.askdirectory(title='Choose save folder') or '')
root.destroy()
"""

_TK_BROWSE_FILE = """
import tkinter as tk
from tkinter import filedialog
root = tk.Tk(); root.withdraw(); root.wm_attributes('-topmost', True)
print(filedialog.askopenfilename(
    title='Select import file',
    filetypes=[('XML / JSON files','*.xml *.json'),('XML files','*.xml'),
               ('JSON files','*.json'),('All files','*.*')]
) or '')
root.destroy()
"""

_TK_CLIPBOARD = """
import tkinter as tk
root = tk.Tk(); root.withdraw()
try:
    print(root.clipboard_get())
except Exception:
    print('')
root.destroy()
"""

@app.route("/api/browse_folder")
def browse_folder():
    path = _run_tk_subprocess(_TK_BROWSE_FOLDER)
    return jsonify(ok=bool(path), path=path)

@app.route("/api/browse_file")
def browse_file():
    path = _run_tk_subprocess(_TK_BROWSE_FILE)
    return jsonify(ok=bool(path), path=path)

@app.route("/api/clipboard")
def read_clipboard():
    text = _run_tk_subprocess(_TK_CLIPBOARD)
    return jsonify(ok=True, text=text)

# ── HTML ───────────────────────────────────────────────────────────────────────
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MangaDex Exporter</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --bg:      #0c0e14;
  --surface: #13161f;
  --card:    #181c28;
  --border:  #252840;
  --accent:  #f0a500;
  --accent2: #7c6af7;
  --green:   #3ecf8e;
  --red:     #f06060;
  --yellow:  #f0c060;
  --text:    #e8eaf6;
  --muted:   #6b7280;
  --mono:    'Space Mono', monospace;
  --sans:    'DM Sans', sans-serif;
  --radius:  10px;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: var(--sans);
       font-size: 14px; display: flex; height: 100vh; overflow: hidden; }

/* ── Sidebar ── */
.sidebar { width: 220px; background: var(--surface); border-right: 1px solid var(--border);
            display: flex; flex-direction: column; padding: 20px 0; flex-shrink: 0; }
.logo { padding: 0 20px 24px; border-bottom: 1px solid var(--border); }
.logo h1 { font-family: var(--mono); font-size: 13px; color: var(--accent);
            letter-spacing: 0.05em; line-height: 1.5; }
.logo small { color: var(--muted); font-size: 11px; }
nav { padding: 16px 8px; flex: 1; }
.nav-item { display: flex; align-items: center; gap: 10px; padding: 10px 14px;
             border-radius: var(--radius); cursor: pointer; color: var(--muted);
             font-weight: 500; font-size: 13px; transition: all .15s; margin-bottom: 2px;
             border: 1px solid transparent; user-select: none; }
.nav-item:hover  { background: var(--card); color: var(--text); }
.nav-item.active { background: var(--card); color: var(--accent);
                   border-color: var(--border); }
.nav-icon { font-size: 16px; width: 20px; text-align: center; }
.sidebar-footer { padding: 16px 20px; border-top: 1px solid var(--border); }
.status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green);
               display: inline-block; margin-right: 6px; }
.status-dot.busy { background: var(--accent); animation: pulse 1s infinite; }
.status-dot.idle { background: var(--muted); }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }

/* ── Main ── */
.main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
.topbar { height: 52px; background: var(--surface); border-bottom: 1px solid var(--border);
           display: flex; align-items: center; padding: 0 24px; gap: 12px; flex-shrink: 0; }
.topbar h2 { font-family: var(--mono); font-size: 12px; color: var(--accent);
              letter-spacing: .12em; text-transform: uppercase; }
.topbar-right { margin-left: auto; display: flex; align-items: center; gap: 10px; }
.badge { background: var(--card); border: 1px solid var(--border); border-radius: 6px;
          padding: 3px 10px; font-size: 11px; color: var(--muted); font-family: var(--mono); }

.content { flex: 1; overflow-y: auto; padding: 20px 24px; }
.page { display: none; }
.page.active { display: block; }

/* ── Cards ── */
.card { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
         padding: 18px; margin-bottom: 16px; }
.card-title { font-family: var(--mono); font-size: 11px; color: var(--accent);
               letter-spacing: .1em; text-transform: uppercase; margin-bottom: 14px;
               display: flex; align-items: center; gap: 8px; }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.three-col { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; }
@media(max-width:900px){ .two-col,.three-col{ grid-template-columns:1fr; } }

/* ── Form ── */
.field { margin-bottom: 12px; }
.field label { display: block; font-size: 11px; color: var(--muted);
                text-transform: uppercase; letter-spacing: .08em; margin-bottom: 6px; }
.input-row { display: flex; gap: 8px; }
input[type=text], input[type=password] {
  background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  color: var(--text); padding: 9px 12px; font-size: 13px; width: 100%;
  font-family: var(--sans); outline: none; transition: border-color .15s; }
input[type=text]:focus, input[type=password]:focus { border-color: var(--accent); }
input[type=text]::placeholder, input[type=password]::placeholder { color: var(--muted); }

/* ── Buttons ── */
.btn { padding: 9px 18px; border-radius: 8px; font-size: 13px; font-weight: 500;
        cursor: pointer; border: 1px solid var(--border); background: var(--surface);
        color: var(--text); font-family: var(--sans); transition: all .15s;
        display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.btn:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.btn:disabled { opacity: .4; cursor: not-allowed; }
.btn-primary { background: var(--accent); color: #000; border-color: var(--accent);
                font-weight: 600; }
.btn-primary:hover:not(:disabled) { background: #ffc107; border-color: #ffc107; color:#000; }
.btn-success { background: var(--green); color: #000; border-color: var(--green); font-weight:600; }
.btn-success:hover:not(:disabled) { background: #5de0a6; }
.btn-danger { background: #3b1515; border-color: var(--red); color: var(--red); }
.btn-danger:hover:not(:disabled) { background: var(--red); color: #000; }
.btn-sm { padding: 5px 12px; font-size: 12px; }
.btn-xs { padding: 3px 8px; font-size: 11px; }

/* ── Mode selector ── */
.mode-group { display: flex; gap: 8px; }
.mode-btn { flex: 1; padding: 10px; border-radius: 8px; border: 1px solid var(--border);
             background: var(--surface); color: var(--muted); cursor: pointer;
             text-align: center; font-size: 12px; font-family: var(--mono);
             transition: all .15s; user-select: none; }
.mode-btn.active { border-color: var(--accent); color: var(--accent); background: rgba(240,165,0,.08); }
.mode-btn .mode-label { font-size: 13px; font-weight: 700; display: block; }
.mode-btn .mode-desc  { font-size: 10px; color: inherit; opacity: .7; display: block; margin-top: 2px; }

/* ── Checkboxes ── */
.checks { display: flex; flex-wrap: wrap; gap: 8px; }
.check-item { display: flex; align-items: center; gap: 8px; padding: 7px 12px;
               border: 1px solid var(--border); border-radius: 8px; cursor: pointer;
               font-size: 12px; user-select: none; transition: all .15s; }
.check-item:hover { border-color: var(--accent2); }
.check-item input { accent-color: var(--accent); width: 14px; height: 14px; cursor: pointer; }

/* ── Status chips ── */
.status-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { padding: 6px 14px; border-radius: 20px; font-size: 12px; font-weight: 600;
         cursor: pointer; border: 1px solid; transition: all .15s; user-select: none; }
.chip[data-status="reading"]      { border-color:#3b82f6; color:#3b82f6; background:rgba(59,130,246,.08); }
.chip[data-status="completed"]    { border-color:#8b5cf6; color:#8b5cf6; background:rgba(139,92,246,.08); }
.chip[data-status="on_hold"]      { border-color:#f59e0b; color:#f59e0b; background:rgba(245,158,11,.08); }
.chip[data-status="dropped"]      { border-color:#ef4444; color:#ef4444; background:rgba(239,68,68,.08); }
.chip[data-status="plan_to_read"] { border-color:#22d3ee; color:#22d3ee; background:rgba(34,211,238,.08); }
.chip[data-status="re_reading"]   { border-color:#ec4899; color:#ec4899; background:rgba(236,72,153,.08); }
.chip:hover { opacity: .8; transform: translateY(-1px); }

/* ── Progress ── */
.progress-wrap { margin-bottom: 8px; }
.progress-bar-bg { background: var(--surface); border-radius: 4px; height: 6px;
                    overflow: hidden; border: 1px solid var(--border); }
.progress-bar-fill { height: 100%; background: linear-gradient(90deg, var(--accent), #ffc107);
                      border-radius: 4px; transition: width .4s ease; width: 0%; }
.progress-meta { display: flex; justify-content: space-between; margin-top: 6px;
                  font-size: 11px; font-family: var(--mono); color: var(--muted); }

/* ── Log ── */
.log-box { background: #080a10; border: 1px solid var(--border); border-radius: var(--radius);
            font-family: var(--mono); font-size: 12px; padding: 14px; height: 280px;
            overflow-y: auto; line-height: 1.7; }
.log-box::-webkit-scrollbar { width: 5px; }
.log-box::-webkit-scrollbar-track { background: transparent; }
.log-box::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
.log-line { display: block; }
.log-ts { color: var(--muted); margin-right: 8px; }
.log-info    { color: var(--text); }
.log-success { color: var(--green); }
.log-error   { color: var(--red); }
.log-warning { color: var(--yellow); }

/* ── Table ── */
.table-wrap { overflow-x: auto; border-radius: var(--radius); border: 1px solid var(--border); }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { background: var(--surface); padding: 10px 14px; text-align: left;
      font-size: 10px; letter-spacing: .1em; text-transform: uppercase;
      color: var(--muted); border-bottom: 1px solid var(--border); font-family: var(--mono); }
td { padding: 9px 14px; border-bottom: 1px solid rgba(37,40,64,.5); }
tr:last-child td { border-bottom: none; }
tr:hover td { background: rgba(37,40,64,.5); }

/* ── File list ── */
.file-item { display: flex; align-items: center; gap: 10px; padding: 10px 14px;
              background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
              margin-bottom: 6px; }
.file-item .file-name { flex: 1; font-family: var(--mono); font-size: 12px;
                          word-break: break-all; }
.file-status-select { background: var(--bg); border: 1px solid var(--border);
                        color: var(--text); border-radius: 6px; padding: 4px 8px;
                        font-size: 12px; cursor: pointer; }

/* ── Toast ── */
#toast { position: fixed; bottom: 24px; right: 24px; background: var(--card);
          border: 1px solid var(--border); border-radius: 10px; padding: 14px 18px;
          font-size: 13px; transform: translateY(80px); opacity: 0;
          transition: all .3s; z-index: 999; max-width: 340px; line-height:1.5; }
#toast.show { transform: translateY(0); opacity: 1; }
#toast.ok   { border-left: 3px solid var(--green); }
#toast.err  { border-left: 3px solid var(--red); }
#toast.warn { border-left: 3px solid var(--yellow); }

/* ── Skipped ── */
.skipped-box { background: rgba(240,96,96,.06); border: 1px solid rgba(240,96,96,.3);
                border-radius: 8px; padding: 12px; font-size: 12px; font-family: var(--mono);
                color: var(--red); max-height: 120px; overflow-y: auto; word-break: break-all; }

/* ── Scrollbar global ── */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
</style>
</head>
<body>

<!-- Sidebar -->
<aside class="sidebar">
  <div class="logo">
    <h1>MANGADEX<br>EXPORTER</h1>
    <small>v2.0 — Web Edition</small>
  </div>
  <nav>
    <div class="nav-item active" data-page="export">
      <span class="nav-icon">📤</span> Export
    </div>
    <div class="nav-item" data-page="convert">
      <span class="nav-icon">🔄</span> Convert
    </div>
    <div class="nav-item" data-page="import">
      <span class="nav-icon">📥</span> Import
    </div>
    <div class="nav-item" data-page="history">
      <span class="nav-icon">📋</span> History
    </div>
    <div class="nav-item" data-page="settings">
      <span class="nav-icon">⚙️</span> Settings
    </div>
  </nav>
  <div class="sidebar-footer">
    <span class="status-dot idle" id="globalDot"></span>
    <span id="globalLabel" style="font-size:12px;color:var(--muted)">Ready</span>
  </div>
</aside>

<!-- Main -->
<div class="main">
  <div class="topbar">
    <h2 id="pageTitle">Export</h2>
    <div class="topbar-right">
      <span class="badge" id="topProgress">—</span>
      <span class="badge" style="color:var(--accent)" id="topEta"></span>
    </div>
  </div>

  <div class="content">

    <!-- ═══ EXPORT PAGE ═══ -->
    <div class="page active" id="page-export">
      <div class="two-col">

        <!-- Left -->
        <div>
          <div class="card">
            <div class="card-title">🔑 Credentials</div>
            <div class="field">
              <label>Client ID</label>
              <div class="input-row">
                <input type="text" id="clientId" placeholder="your-client-id">
                <button class="btn btn-sm" onclick="paste('clientId')">Paste</button>
              </div>
            </div>
            <div class="field">
              <label>Client Secret</label>
              <div class="input-row">
                <input type="password" id="clientSecret" placeholder="••••••••">
                <button class="btn btn-sm" onclick="paste('clientSecret')">Paste</button>
              </div>
            </div>
            <div class="field">
              <label>Username</label>
              <div class="input-row">
                <input type="text" id="username" placeholder="mangadex username">
                <button class="btn btn-sm" onclick="paste('username')">Paste</button>
              </div>
            </div>
            <div class="field">
              <label>Password</label>
              <div class="input-row">
                <input type="password" id="password" placeholder="••••••••">
                <button class="btn btn-sm" onclick="paste('password')">Paste</button>
              </div>
            </div>
            <button class="btn" onclick="testCreds()" style="margin-top:4px">✓ Test Credentials</button>
          </div>

          <div class="card">
            <div class="card-title">👤 MAL Info <small style="font-weight:300;color:var(--muted)">(for XML header)</small></div>
            <div class="field">
              <label>MAL User ID</label>
              <div class="input-row">
                <input type="text" id="malUserId" placeholder="Find in your MAL export XML">
                <button class="btn btn-sm" onclick="paste('malUserId')">Paste</button>
              </div>
            </div>
            <div class="field">
              <label>MAL Username</label>
              <div class="input-row">
                <input type="text" id="malUsername" placeholder="your MAL username">
                <button class="btn btn-sm" onclick="paste('malUsername')">Paste</button>
              </div>
            </div>
          </div>

          <div class="card">
            <div class="card-title">⚙️ Options</div>
            <div class="field">
              <label>Mode</label>
              <div class="mode-group">
                <div class="mode-btn active" data-mode="fast" onclick="setMode('fast')">
                  <span class="mode-label">⚡ Fast</span>
                  <span class="mode-desc">Status only — minutes</span>
                </div>
                <div class="mode-btn" data-mode="deep" onclick="setMode('deep')">
                  <span class="mode-label">🔍 Deep</span>
                  <span class="mode-desc">Last chapter — slower</span>
                </div>
              </div>
            </div>
            <div class="field">
              <label>Save Folder</label>
              <div class="input-row">
                <input type="text" id="saveDir" placeholder="/path/to/folder">
                <button class="btn btn-sm" onclick="browseFolder('saveDir')">📁 Browse</button>
              </div>
            </div>
            <div class="field">
              <label>Export Formats</label>
              <div class="checks">
                <label class="check-item"><input type="checkbox" id="fmtMal" checked> MAL XML + .gz</label>
                <label class="check-item"><input type="checkbox" id="fmtAl" checked> AniList XML</label>
                <label class="check-item"><input type="checkbox" id="fmtJson" checked> JSON backup</label>
                <label class="check-item"><input type="checkbox" id="dryRun"> 🔍 Dry Run</label>
              </div>
            </div>
          </div>
        </div>

        <!-- Right -->
        <div>
          <div class="card">
            <div class="card-title">🚀 Extract</div>
            <button class="btn btn-primary" style="width:100%;font-size:15px;padding:14px;margin-bottom:12px"
                    id="btnAll" onclick="startExport(null)">
              ⚡ Extract Entire Library
            </button>
            <div class="status-chips" id="statusChips">
              <div class="chip" data-status="reading"      onclick="startExport('reading')">Reading</div>
              <div class="chip" data-status="completed"    onclick="startExport('completed')">Completed</div>
              <div class="chip" data-status="on_hold"      onclick="startExport('on_hold')">On-Hold</div>
              <div class="chip" data-status="dropped"      onclick="startExport('dropped')">Dropped</div>
              <div class="chip" data-status="plan_to_read" onclick="startExport('plan_to_read')">Plan to Read</div>
              <div class="chip" data-status="re_reading"   onclick="startExport('re_reading')">Re-reading</div>
            </div>
            <div style="display:flex;gap:8px;margin-top:12px">
              <button class="btn" id="btnResume" onclick="resumeExport()" style="flex:1" disabled>
                ▶ Resume
              </button>
              <button class="btn btn-danger" id="btnStop" onclick="stopExport()" style="flex:1" disabled>
                ⏹ Stop
              </button>
            </div>
          </div>

          <div class="card">
            <div class="card-title">📊 Progress</div>
            <div class="progress-wrap">
              <div class="progress-bar-bg">
                <div class="progress-bar-fill" id="progFill"></div>
              </div>
              <div class="progress-meta">
                <span id="progLabel">Ready</span>
                <span id="progEta"></span>
              </div>
            </div>
          </div>

          <div class="card" style="flex:1">
            <div class="card-title" style="justify-content:space-between">
              📝 Log
              <button class="btn btn-xs" onclick="clearLog()">Clear</button>
            </div>
            <div class="log-box" id="logBox"></div>
          </div>

          <div class="card" id="skippedCard" style="display:none">
            <div class="card-title" style="justify-content:space-between">
              ⚠️ No MAL ID — Add Manually
              <span id="skippedCount" style="font-size:11px;color:var(--yellow);font-family:var(--mono)"></span>
            </div>
            <div style="font-size:11px;color:var(--muted);margin-bottom:8px">
              These manga were skipped because MangaDex has no MAL link for them. Add them manually on MAL/AniList.
            </div>
            <div id="skippedList" style="
              display:flex;flex-wrap:wrap;gap:6px;max-height:160px;
              overflow-y:auto;padding:4px 0;
            "></div>
          </div>
        </div>
      </div>
    </div>

    <!-- ═══ CONVERT PAGE ═══ -->
    <div class="page" id="page-convert">
      <div class="two-col">
        <div>
          <div class="card">
            <div class="card-title">👤 MAL Info</div>
            <div class="field">
              <label>MAL User ID</label>
              <div class="input-row">
                <input type="text" id="convMalId" placeholder="from your MAL export XML">
                <button class="btn btn-sm" onclick="paste('convMalId')">Paste</button>
              </div>
            </div>
            <div class="field">
              <label>MAL Username</label>
              <div class="input-row">
                <input type="text" id="convMalName" placeholder="your MAL username">
                <button class="btn btn-sm" onclick="paste('convMalName')">Paste</button>
              </div>
            </div>
          </div>
          <div class="card">
            <div class="card-title">⚙️ Options</div>
            <div class="field">
              <label>Output Folder</label>
              <div class="input-row">
                <input type="text" id="convSaveDir" placeholder="/path/to/folder">
                <button class="btn btn-sm" onclick="browseFolder('convSaveDir')">📁 Browse</button>
              </div>
            </div>
            <div class="checks" style="flex-direction:column;gap:6px">
              <label class="check-item"><input type="checkbox" id="convMal" checked> MAL XML + .gz</label>
              <label class="check-item"><input type="checkbox" id="convAl" checked> AniList XML</label>
              <label class="check-item"><input type="checkbox" id="convScores" checked> Include Scores</label>
              <label class="check-item"><input type="checkbox" id="convDry"> 🔍 Dry Run</label>
            </div>
          </div>
          <div class="card">
            <div class="card-title">⚡ Generate</div>
            <button class="btn btn-success" style="width:100%;font-size:15px;padding:14px"
                    onclick="generateXml()">⚡ Generate XML Files</button>
            <div id="convResult" style="margin-top:12px;font-size:13px;color:var(--muted)"></div>
          </div>
        </div>
        <div>
          <div class="card">
            <div class="card-title" style="justify-content:space-between">
              📁 Excel Files
              <div style="display:flex;gap:6px">
                <button class="btn btn-xs" onclick="addFile()">+ Add</button>
                <button class="btn btn-xs" onclick="autoFill()">⟳ Auto-fill</button>
                <button class="btn btn-xs btn-danger" onclick="clearFiles()">✕ Clear</button>
              </div>
            </div>
            <div id="fileList" style="min-height:60px"></div>
            <div style="color:var(--muted);font-size:11px;margin-top:8px">
              Add the .xlsx files from the Export step. Status is auto-detected from filename.
            </div>
          </div>
          <div class="card">
            <div class="card-title">⚠️ Skipped Manga</div>
            <div id="skippedBox" class="skipped-box">—</div>
          </div>
        </div>
      </div>
    </div>

    <!-- ═══ IMPORT PAGE ═══ -->
    <div class="page" id="page-import">
      <div class="two-col">
        <div>
          <div class="card">
            <div class="card-title">🔑 Credentials</div>
            <div class="field">
              <label>Client ID</label>
              <div class="input-row">
                <input type="text" id="impClientId" placeholder="your-client-id">
                <button class="btn btn-sm" onclick="paste('impClientId')">Paste</button>
              </div>
            </div>
            <div class="field">
              <label>Client Secret</label>
              <div class="input-row">
                <input type="password" id="impClientSecret" placeholder="••••••••">
                <button class="btn btn-sm" onclick="paste('impClientSecret')">Paste</button>
              </div>
            </div>
            <div class="field">
              <label>Username</label>
              <div class="input-row">
                <input type="text" id="impUsername" placeholder="mangadex username">
                <button class="btn btn-sm" onclick="paste('impUsername')">Paste</button>
              </div>
            </div>
            <div class="field">
              <label>Password</label>
              <div class="input-row">
                <input type="password" id="impPassword" placeholder="••••••••">
                <button class="btn btn-sm" onclick="paste('impPassword')">Paste</button>
              </div>
            </div>
          </div>

          <div class="card">
            <div class="card-title">📂 Source File</div>
            <div class="field">
              <label>File Type</label>
              <div class="mode-group">
                <div class="mode-btn active" data-imptype="xml" onclick="setImpType('xml')">
                  <span class="mode-label">📄 MAL / AniList XML</span>
                  <span class="mode-desc">mal_*.xml or anilist_*.xml</span>
                </div>
                <div class="mode-btn" data-imptype="json" onclick="setImpType('json')">
                  <span class="mode-label">🗂 JSON Backup</span>
                  <span class="mode-desc">mdex_*.json from Export tab</span>
                </div>
              </div>
            </div>
            <div class="field">
              <label>File Path</label>
              <div class="input-row">
                <input type="text" id="impFilePath" placeholder="/path/to/file.xml or file.json">
                <button class="btn btn-sm" onclick="browseFile('impFilePath')">📁 Browse</button>
                <button class="btn btn-sm" onclick="paste('impFilePath')">Paste</button>
              </div>
            </div>
            <div id="impXmlNote" style="font-size:12px;color:var(--muted);margin-top:4px;line-height:1.6">
              ⚠️ <strong style="color:var(--yellow)">XML import is slow</strong> — each manga requires a MangaDex API lookup by MAL ID.
              For large libraries (500+ manga) expect 10–30 min. Use JSON import when possible for instant speed.
            </div>
            <div id="impJsonNote" style="font-size:12px;color:var(--muted);margin-top:4px;line-height:1.6;display:none">
              ✓ <strong style="color:var(--green)">JSON import is fast</strong> — uses MangaDex UUIDs directly from the backup, no extra lookups needed.
            </div>
          </div>

          <div class="card">
            <div class="card-title">⚙️ Options</div>
            <div class="checks" style="flex-direction:column;gap:6px">
              <label class="check-item"><input type="checkbox" id="impScores" checked> Import Scores / Ratings</label>
              <label class="check-item"><input type="checkbox" id="impDry"> 🔍 Dry Run (simulate, no changes made)</label>
            </div>
          </div>
        </div>

        <div>
          <div class="card">
            <div class="card-title">📥 Import</div>
            <p style="font-size:13px;color:var(--muted);margin-bottom:14px;line-height:1.6">
              This will set manga statuses and ratings on your MangaDex account.
              Existing entries will be updated. Manga not found on MangaDex will be skipped.
            </p>
            <button class="btn btn-primary" style="width:100%;font-size:15px;padding:14px;margin-bottom:12px"
                    id="btnImport" onclick="startImport()">
              📥 Start Import
            </button>
            <button class="btn btn-danger" id="btnImportStop" onclick="stopExport()" style="width:100%" disabled>
              ⏹ Stop
            </button>
          </div>

          <div class="card">
            <div class="card-title">📊 Progress</div>
            <div class="progress-wrap">
              <div class="progress-bar-bg">
                <div class="progress-bar-fill" id="impProgFill"></div>
              </div>
              <div class="progress-meta">
                <span id="impProgLabel">Ready</span>
              </div>
            </div>
          </div>

          <div class="card">
            <div class="card-title" style="justify-content:space-between">
              📝 Log
              <button class="btn btn-xs" onclick="clearLog()">Clear</button>
            </div>
            <div class="log-box" id="logBox2"></div>
          </div>

          <div class="card" id="impSkippedCard" style="display:none">
            <div class="card-title">
              ⚠️ Skipped
              <span id="impSkippedCount" style="font-size:11px;color:var(--yellow);font-family:var(--mono);margin-left:8px"></span>
            </div>
            <div id="impSkippedList" style="display:flex;flex-wrap:wrap;gap:6px;max-height:140px;overflow-y:auto;padding:4px 0"></div>
          </div>
        </div>
      </div>
    </div>

    <!-- ═══ HISTORY PAGE ═══ -->
    <div class="page" id="page-history">
      <div class="card">
        <div class="card-title" style="justify-content:space-between">
          📋 Export History
          <button class="btn btn-xs" onclick="loadHistory()">🔄 Refresh</button>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>Date</th><th>Type</th><th>Total</th><th>Skipped</th>
              <th>Mode</th><th>Elapsed</th><th>Files</th>
            </tr></thead>
            <tbody id="historyBody">
              <tr><td colspan="7" style="color:var(--muted);text-align:center;padding:30px">
                No history yet
              </td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ═══ SETTINGS PAGE ═══ -->
    <div class="page" id="page-settings">
      <div class="two-col">
        <div>
          <div class="card">
            <div class="card-title">🎯 Default Mode</div>
            <div class="mode-group">
              <div class="mode-btn active" data-smode="fast" onclick="setSettingsMode('fast')">
                <span class="mode-label">⚡ Fast</span>
                <span class="mode-desc">Status + title only<br>Recommended for most users</span>
              </div>
              <div class="mode-btn" data-smode="deep" onclick="setSettingsMode('deep')">
                <span class="mode-label">🔍 Deep</span>
                <span class="mode-desc">Last read chapter<br>Slower but more accurate</span>
              </div>
            </div>
          </div>
          <div class="card">
            <div class="card-title">🔖 Checkpoint</div>
            <p style="font-size:13px;color:var(--muted);margin-bottom:12px">
              If an export was interrupted, you can resume it from the Export tab.
              Clear the checkpoint if you want to start fresh.
            </p>
            <button class="btn btn-danger" onclick="clearCheckpoint()">✕ Clear Checkpoint</button>
            <div id="cpInfo" style="margin-top:10px;font-size:12px;color:var(--muted)"></div>
          </div>
        </div>
        <div>
          <div class="card">
            <div class="card-title">ℹ️ About</div>
            <p style="font-size:13px;color:var(--muted);line-height:1.7">
              <strong style="color:var(--text)">MangaDex All-in-One Exporter v2.0</strong><br><br>
              Export your full MangaDex library to MAL and AniList XML, plus JSON backup.<br><br>
              <strong style="color:var(--accent)">Fast mode</strong> — skips individual chapter fetching. Much faster, recommended for most users.<br><br>
              <strong style="color:var(--accent2)">Deep mode</strong> — fetches your last read chapter per manga. Slower but includes progress data.<br><br>
              Running locally at <code style="background:var(--surface);padding:2px 6px;border-radius:4px">http://localhost:7337</code>
            </p>
          </div>
        </div>
      </div>
    </div>

  </div><!-- /content -->
</div><!-- /main -->

<div id="toast"></div>

<script>
// ── State ───────────────────────────────────────────────────────────────────
let mode = 'fast';
let convFiles = [];  // [{path, status}]
let pollTimer = null;

// ── Navigation ──────────────────────────────────────────────────────────────
document.querySelectorAll('.nav-item').forEach(el => {
  el.addEventListener('click', () => {
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    el.classList.add('active');
    const pg = el.dataset.page;
    document.getElementById('page-'+pg).classList.add('active');
    document.getElementById('pageTitle').textContent = el.textContent.trim();
    if (pg === 'history') loadHistory();
    if (pg === 'settings') loadCpInfo();
  });
});

// ── SSE Log stream ───────────────────────────────────────────────────────────
const evtSrc = new EventSource('/api/stream');
evtSrc.onmessage = e => {
  const d = JSON.parse(e.data);
  if (d.ping) return;
  addLog(d.ts, d.msg, d.tag);
};

function addLog(ts, msg, tag) {
  const box = document.getElementById('logBox');
  const line = document.createElement('span');
  line.className = 'log-line';
  line.innerHTML = `<span class="log-ts">[${ts}]</span><span class="log-${tag||'info'}">${escHtml(msg)}</span>`;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function clearLog() {
  document.getElementById('logBox').innerHTML = '';
}

// ── Poll status ─────────────────────────────────────────────────────────────
async function pollStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    const fill = document.getElementById('progFill');
    const lbl  = document.getElementById('progLabel');
    const eta  = document.getElementById('progEta');
    const dot  = document.getElementById('globalDot');
    const glbl = document.getElementById('globalLabel');
    const tp   = document.getElementById('topProgress');
    const te   = document.getElementById('topEta');

    fill.style.width = d.progress + '%';
    lbl.textContent  = d.label;
    eta.textContent  = d.eta;
    tp.textContent   = d.progress > 0 ? Math.round(d.progress) + '%' : '—';
    te.textContent   = d.eta;

    // Mirror progress to import page bars too
    const impFill  = document.getElementById('impProgFill');
    const impLabel = document.getElementById('impProgLabel');
    if (impFill)  impFill.style.width   = d.progress + '%';
    if (impLabel) impLabel.textContent  = d.label;

    if (d.running) {
      dot.className = 'status-dot busy';
      glbl.textContent = 'Running…';
      document.getElementById('btnAll').disabled = true;
      document.getElementById('btnStop').disabled = false;
      document.getElementById('btnResume').disabled = true;
      document.querySelectorAll('.chip').forEach(c => c.style.pointerEvents='none');
      const bi = document.getElementById('btnImport');
      const bs = document.getElementById('btnImportStop');
      if (bi) bi.disabled = true;
      if (bs) bs.disabled = false;
    } else {
      dot.className = 'status-dot' + (d.progress >= 100 ? '' : ' idle');
      glbl.textContent = d.progress >= 100 ? 'Done!' : 'Ready';
      document.getElementById('btnAll').disabled = false;
      document.getElementById('btnStop').disabled = true;
      document.getElementById('btnResume').disabled = !d.has_checkpoint;
      document.querySelectorAll('.chip').forEach(c => c.style.pointerEvents='');
      const bi = document.getElementById('btnImport');
      const bs = document.getElementById('btnImportStop');
      if (bi) bi.disabled = false;
      if (bs) bs.disabled = true;
    }

    // Skipped manga panel (export page)
    const skipped = d.skipped || [];
    const card = document.getElementById('skippedCard');
    const list = document.getElementById('skippedList');
    const cnt  = document.getElementById('skippedCount');
    if (skipped.length) {
      card.style.display = '';
      cnt.textContent = skipped.length + ' manga';
      list.innerHTML = skipped.map(t =>
        `<span style="background:rgba(240,96,96,.1);border:1px solid rgba(240,96,96,.3);
         border-radius:5px;padding:3px 8px;font-size:11px;color:var(--red);
         font-family:var(--mono);white-space:nowrap">${escHtml(t)}</span>`
      ).join('');
    } else {
      card.style.display = 'none';
    }

    // Skipped panel on import page
    const impCard = document.getElementById('impSkippedCard');
    const impList = document.getElementById('impSkippedList');
    const impCnt  = document.getElementById('impSkippedCount');
    if (skipped.length && impCard) {
      impCard.style.display = '';
      impCnt.textContent = skipped.length + ' skipped';
      impList.innerHTML = skipped.map(t =>
        `<span style="background:rgba(240,96,96,.1);border:1px solid rgba(240,96,96,.3);
         border-radius:5px;padding:3px 8px;font-size:11px;color:var(--red);
         font-family:var(--mono);white-space:nowrap">${escHtml(t)}</span>`
      ).join('');
    } else if (impCard) {
      impCard.style.display = 'none';
    }
  } catch(e) {}
}
setInterval(pollStatus, 800);
pollStatus();

// ── Mode ─────────────────────────────────────────────────────────────────────
function setMode(m) {
  mode = m;
  document.querySelectorAll('.mode-btn[data-mode]').forEach(b => {
    b.classList.toggle('active', b.dataset.mode === m);
  });
}
function setSettingsMode(m) {
  setMode(m);
  document.querySelectorAll('.mode-btn[data-smode]').forEach(b => {
    b.classList.toggle('active', b.dataset.smode === m);
  });
}

// ── Credentials ──────────────────────────────────────────────────────────────
function creds() {
  return {
    client_id:     document.getElementById('clientId').value.trim(),
    client_secret: document.getElementById('clientSecret').value.trim(),
    username:      document.getElementById('username').value.trim(),
    password:      document.getElementById('password').value.trim(),
    mal_user_id:   document.getElementById('malUserId').value.trim(),
    mal_username:  document.getElementById('malUsername').value.trim(),
    save_dir:      document.getElementById('saveDir').value.trim(),
    mode:          mode,
    fmt_mal:       document.getElementById('fmtMal').checked,
    fmt_al:        document.getElementById('fmtAl').checked,
    fmt_json:      document.getElementById('fmtJson').checked,
    dry_run:       document.getElementById('dryRun').checked,
  };
}

async function testCreds() {
  const c = creds();
  if (!c.client_id || !c.client_secret || !c.username || !c.password) {
    toast('Fill in all credential fields first.', 'err'); return;
  }
  toast('Testing…', 'warn');
  const r = await fetch('/api/export', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({...c, dry_run: true, status: 'reading', _test_only: true})
  });
  // Just start a dry run of reading status as a credential test
  toast('Credentials test started — check the log!', 'ok');
}

// ── Export ───────────────────────────────────────────────────────────────────
async function startExport(status) {
  const c = creds();
  if (!c.client_id || !c.username) { toast('Fill in credentials first.', 'err'); return; }
  if (status) c.status = status;
  const r = await fetch('/api/export', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(c)
  });
  const d = await r.json();
  if (!d.ok) { toast(d.error || 'Error', 'err'); return; }
  toast('Export started!', 'ok');
}

async function resumeExport() {
  const c = creds();
  if (!c.client_id || !c.username) { toast('Fill in credentials first.', 'err'); return; }
  const r = await fetch('/api/resume', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(c)
  });
  const d = await r.json();
  if (!d.ok) { toast(d.error || 'Error', 'err'); return; }
  toast('Resuming from checkpoint…', 'ok');
}

async function stopExport() {
  await fetch('/api/stop', {method:'POST'});
  toast('Stop requested…', 'warn');
}

// ── Convert ──────────────────────────────────────────────────────────────────
function addFile() {
  const path = prompt('Paste the full path to your .xlsx file:');
  if (!path) return;
  const status = guessStatus(path);
  convFiles.push({path, status});
  renderFileList();
}

async function autoFill() {
  const r = await fetch('/api/exported_files');
  const files = await r.json();
  if (!files.length) { toast('No recently exported files found. Export first.', 'warn'); return; }
  files.forEach(path => {
    if (!convFiles.find(f => f.path === path)) {
      convFiles.push({path, status: guessStatus(path)});
    }
  });
  renderFileList();
  toast(`${files.length} file(s) added!`, 'ok');
  // also fill save dir
  if (files.length) {
    document.getElementById('convSaveDir').value = files[0].replace(/[/\\][^/\\]+$/, '');
  }
}

function clearFiles() { convFiles = []; renderFileList(); }

function guessStatus(path) {
  const n = path.toLowerCase();
  if (n.includes('re_reading') || n.includes('re-reading')) return 'Reading';
  if (n.includes('reading'))      return 'Reading';
  if (n.includes('completed'))    return 'Completed';
  if (n.includes('on_hold'))      return 'On-Hold';
  if (n.includes('dropped'))      return 'Dropped';
  if (n.includes('plan'))         return 'Plan to Read';
  return 'Reading';
}

function renderFileList() {
  const el = document.getElementById('fileList');
  if (!convFiles.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:10px 0">No files added yet. Click + Add or ⟳ Auto-fill.</div>';
    return;
  }
  el.innerHTML = convFiles.map((f, i) => `
    <div class="file-item">
      <span class="nav-icon">📄</span>
      <span class="file-name">${f.path.split(/[/\\]/).pop()}</span>
      <select class="file-status-select" onchange="convFiles[${i}].status=this.value">
        ${['Reading','Completed','On-Hold','Dropped','Plan to Read'].map(s =>
          `<option ${s===f.status?'selected':''}>${s}</option>`).join('')}
      </select>
      <button class="btn btn-xs btn-danger" onclick="convFiles.splice(${i},1);renderFileList()">✕</button>
    </div>`).join('');
}

async function generateXml() {
  if (!convFiles.length) { toast('Add files first.', 'err'); return; }
  const uid = document.getElementById('convMalId').value.trim();
  const uname = document.getElementById('convMalName').value.trim();
  if (!uid || !uname) { toast('MAL User ID and Username required.', 'err'); return; }

  const payload = {
    mal_user_id: uid, mal_username: uname,
    save_dir:    document.getElementById('convSaveDir').value.trim(),
    fmt_mal:     document.getElementById('convMal').checked,
    fmt_al:      document.getElementById('convAl').checked,
    include_scores: document.getElementById('convScores').checked,
    dry_run:     document.getElementById('convDry').checked,
    files:       convFiles,
  };

  const r = await fetch('/api/convert', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(payload)
  });
  const d = await r.json();

  const res = document.getElementById('convResult');
  const sk  = document.getElementById('skippedBox');

  if (!d.ok) { toast(d.error || 'Error', 'err'); return; }

  if (d.dry) {
    res.innerHTML = `<span style="color:var(--yellow)">🔍 Dry Run: ${d.total} manga would be exported, ${d.skipped} skipped.</span>`;
    toast('Dry run complete!', 'warn');
  } else {
    res.innerHTML = `<span style="color:var(--green)">✓ ${d.total} manga exported. ${d.skipped} skipped.</span>`;
    toast(`Done! ${d.total} manga → ${d.files?.length} file(s)`, 'ok');
  }

  if (d.skipped_titles?.length) {
    sk.textContent = `${d.skipped} skipped:\n` + d.skipped_titles.join(', ');
  } else {
    sk.textContent = '✓ All manga had MAL IDs!';
    sk.style.color = 'var(--green)';
  }
}

// ── History ──────────────────────────────────────────────────────────────────
async function loadHistory() {
  const r = await fetch('/api/history');
  const rows = await r.json();
  const tbody = document.getElementById('historyBody');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="7" style="color:var(--muted);text-align:center;padding:30px">No history yet</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(e => `<tr>
    <td style="font-family:var(--mono);font-size:11px">${e.date||''}</td>
    <td>${e.type||''}</td>
    <td style="color:var(--green)">${e.total||''}</td>
    <td style="color:var(--red)">${e.skipped||0}</td>
    <td><span style="background:var(--surface);padding:2px 8px;border-radius:4px;font-size:11px">${e.mode||''}</span></td>
    <td style="color:var(--muted)">${e.elapsed||''}</td>
    <td style="font-family:var(--mono);font-size:10px;color:var(--muted)">${(e.files||'').substring(0,60)}${(e.files||'').length>60?'…':''}</td>
  </tr>`).join('');
}

// ── Settings ─────────────────────────────────────────────────────────────────
async function clearCheckpoint() {
  await fetch('/api/checkpoint/clear', {method:'POST'});
  toast('Checkpoint cleared.', 'ok');
  loadCpInfo();
}

async function loadCpInfo() {
  const r = await fetch('/api/checkpoint');
  const d = await r.json();
  const el = document.getElementById('cpInfo');
  if (d.timestamp) {
    el.innerHTML = `Checkpoint found from <strong>${d.timestamp}</strong>. Completed: ${(d.completed||[]).join(', ') || 'none'}`;
  } else {
    el.innerHTML = 'No checkpoint on disk.';
  }
}

// ── Helpers ──────────────────────────────────────────────────────────────────
async function paste(id) {
  try {
    // Use server-side clipboard read to avoid pywebview/browser permission issues
    const r = await fetch('/api/clipboard');
    const d = await r.json();
    if (d.ok && d.text) {
      document.getElementById(id).value = d.text;
    } else {
      // Fallback to browser clipboard API if server-side fails
      try {
        const text = await navigator.clipboard.readText();
        document.getElementById(id).value = text;
      } catch(e2) { toast('Clipboard access denied — paste manually.', 'warn'); }
    }
  } catch(e) { toast('Clipboard access denied — paste manually.', 'warn'); }
}

async function browseFolder(inputId) {
  try {
    const r = await fetch('/api/browse_folder');
    const d = await r.json();
    if (d.ok && d.path) {
      document.getElementById(inputId).value = d.path;
      toast('Folder selected!', 'ok');
    }
  } catch(e) { toast('Could not open folder picker.', 'err'); }
}

async function browseFile(inputId) {
  try {
    const r = await fetch('/api/browse_file');
    const d = await r.json();
    if (d.ok && d.path) {
      document.getElementById(inputId).value = d.path;
      toast('File selected!', 'ok');
    }
  } catch(e) { toast('Could not open file picker.', 'err'); }
}

let toastTimer = null;
function toast(msg, type='ok') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = `show ${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3500);
}

// ── Import ───────────────────────────────────────────────────────────────────
let impType = 'xml';

function setImpType(t) {
  impType = t;
  document.querySelectorAll('.mode-btn[data-imptype]').forEach(b => {
    b.classList.toggle('active', b.dataset.imptype === t);
  });
  document.getElementById('impXmlNote').style.display  = t === 'xml'  ? '' : 'none';
  document.getElementById('impJsonNote').style.display = t === 'json' ? '' : 'none';
}

async function startImport() {
  const cid  = document.getElementById('impClientId').value.trim();
  const csec = document.getElementById('impClientSecret').value.trim();
  const user = document.getElementById('impUsername').value.trim();
  const pwd  = document.getElementById('impPassword').value.trim();
  const fp   = document.getElementById('impFilePath').value.trim();

  if (!cid || !csec || !user || !pwd) { toast('Fill in all credentials.', 'err'); return; }
  if (!fp) { toast('Provide a file path.', 'err'); return; }

  const payload = {
    client_id: cid, client_secret: csec,
    username: user, password: pwd,
    file_path: fp, file_type: impType,
    import_scores: document.getElementById('impScores').checked,
    dry_run: document.getElementById('impDry').checked,
  };

  const r = await fetch('/api/import', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  const d = await r.json();
  if (!d.ok) { toast(d.error || 'Error', 'err'); return; }
  toast('Import started!', 'ok');
}

// Mirror SSE log to the import log box too
evtSrc.addEventListener('message', e => {
  const d = JSON.parse(e.data);
  if (d.ping) return;
  const box2 = document.getElementById('logBox2');
  if (!box2) return;
  const line = document.createElement('span');
  line.className = 'log-line';
  line.innerHTML = `<span class="log-ts">[${d.ts}]</span><span class="log-${d.tag||'info'}">${escHtml(d.msg)}</span>`;
  box2.appendChild(line);
  box2.scrollTop = box2.scrollHeight;
});

// ── Init ─────────────────────────────────────────────────────────────────────
renderFileList();
loadHistory();
</script>
</body>
</html>"""

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import threading, webbrowser, time
    import requests as _r
    PORT = 7337
    threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True),
        daemon=True
    ).start()
    for _ in range(20):
        try: _r.get(f"http://127.0.0.1:{PORT}/", timeout=1); break
        except Exception: time.sleep(0.3)
    print(f"\n  MangaDex Exporter — running at http://localhost:{PORT}\n")
    webbrowser.open(f"http://localhost:{PORT}")
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down.")
