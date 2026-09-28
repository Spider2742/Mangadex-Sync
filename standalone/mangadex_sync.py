#!/usr/bin/env python3
# This file is generated from `pypi pkg/mangadex_sync/app.py` by
# scripts/sync_standalone.py — do not edit it directly.
# Edit the source file and re-run that script instead.
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
APP_VERSION = "2.2.1"
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
        self._username = self._password = None
        self.expires_at = None
        self._rate_delay = 0.25

    def auth(self, cid, csec, user, pwd):
        self.client_id, self.client_secret = cid, csec
        self._username, self._password = user, pwd
        payload = dict(grant_type="password", username=user, password=pwd,
                       client_id=cid, client_secret=csec)
        for attempt in range(MAX_RETRY):
            try:
                r = self.session.post(AUTH_URL, data=payload, timeout=20)
                if r.status_code == 200:
                    self._store(r.json()); return True
                return False
            except Exception:
                if attempt < MAX_RETRY-1: time.sleep(2)
        return False

    def _store(self, d):
        self.access_token = d["access_token"]
        self.refresh_token = d.get("refresh_token")
        self.expires_at = datetime.now() + timedelta(seconds=d.get("expires_in",900)-60)
        self.session.headers["Authorization"] = f"Bearer {self.access_token}"

    def _ensure(self):
        if self.expires_at and datetime.now() >= self.expires_at:
            # Try refresh token first
            try:
                r = self.session.post(AUTH_URL, timeout=20, data=dict(
                    grant_type="refresh_token", refresh_token=self.refresh_token,
                    client_id=self.client_id, client_secret=self.client_secret))
                if r.status_code == 200:
                    self._store(r.json()); return
            except Exception: pass
            # Refresh failed — try re-auth with stored credentials
            if self._username and self._password:
                try:
                    r = self.session.post(AUTH_URL, timeout=20, data=dict(
                        grant_type="password", username=self._username,
                        password=self._password, client_id=self.client_id,
                        client_secret=self.client_secret))
                    if r.status_code == 200: self._store(r.json())
                except Exception: pass

    @staticmethod
    def _retry_wait(r):
        """Compute how long to back off after a 429.
        MangaDex sends X-RateLimit-Retry-After as a unix epoch timestamp
        (not a delta), so read that first; fall back to a plain Retry-After
        delta, then to a flat 5s. Clamped to 1-60s so a bad/garbage header
        can't stall or skip the wait entirely."""
        for header, is_epoch in (("X-RateLimit-Retry-After", True), ("Retry-After", False)):
            raw = r.headers.get(header)
            if raw is None: continue
            try:
                val = float(raw)
                wait = val - time.time() if is_epoch else val
                return max(1, min(60, int(round(wait))))
            except (TypeError, ValueError):
                continue
        return 5

    def get(self, url, params=None):
        self._ensure()
        for attempt in range(MAX_RETRY):
            try:
                r = self.session.get(url, params=params, timeout=30)
                if r.status_code == 200: return r.json()
                if r.status_code == 429:
                    wait = self._retry_wait(r)
                    self._rate_delay = min(self._rate_delay * 2, 5.0)
                    time.sleep(wait)
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
            d = self.get(f"{API_BASE}/manga", {"ids[]":batch,"limit":100,"includes[]":["author"],
                "contentRating[]":["safe","suggestive","erotica","pornographic"]})
            if d:
                for m in d.get("data",[]): out[m["id"]] = m
            if cb: cb(min((i+1)*bs,len(ids)), len(ids))
            time.sleep(self._rate_delay)
        return out

    def read_chapters(self, ids):
        out, bs = {}, 100
        for batch in [ids[i:i+bs] for i in range(0,len(ids),bs)]:
            d = self.get(f"{API_BASE}/manga/read", {"ids[]":batch,"grouped":"true"})
            if d: out.update(d.get("data",{}))
            time.sleep(self._rate_delay)
        return out

    def chapter_details(self, ids, cb=None):
        out, bs = {}, 100
        for i, batch in enumerate([ids[j:j+bs] for j in range(0,len(ids),bs)]):
            d = self.get(f"{API_BASE}/chapter", {"ids[]":batch,"limit":100,
                "includeUnavailable":1,
                "contentRating[]":["safe","suggestive","erotica","pornographic"]})
            if d:
                for ch in d.get("data",[]): out[ch["id"]] = ch
            if cb: cb(min((i+1)*bs,len(ids)), len(ids))
            time.sleep(self._rate_delay)
        return out

    def ratings(self, ids):
        out, bs = {}, 100
        for batch in [ids[i:i+bs] for i in range(0,len(ids),bs)]:
            d = self.get(f"{API_BASE}/rating", {"manga[]":batch})
            if d:
                ratings_data = d.get("ratings") or {}
                if isinstance(ratings_data, dict):
                    for mid, rd in ratings_data.items():
                        out[mid] = rd.get("rating",0)
            time.sleep(self._rate_delay)
        return out

    def put(self, url, body):
        self._ensure()
        for attempt in range(MAX_RETRY):
            try:
                r = self.session.put(url, json=body, timeout=20)
                if r.status_code in (200, 204): return True
                if r.status_code == 429:
                    time.sleep(self._retry_wait(r))
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
                    time.sleep(self._retry_wait(r))
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
                    time.sleep(self._retry_wait(r))
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
                    time.sleep(self._retry_wait(r))
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

    def find_by_link(self, key, value, title):
        """Find a MangaDex UUID for an external ID (key "mal" or "al").
        MangaDex has no reverse lookup: /manga rejects links[...] filters with
        a 400. So search by title and accept only a result whose own
        links[key] matches exactly; never guess on a title match alone."""
        if not value or not title:
            return None
        d = self.get(f"{API_BASE}/manga", {"title": title, "limit": 10,
            "order[relevance]": "desc",
            "contentRating[]": ["safe","suggestive","erotica","pornographic"]})
        for m in (d or {}).get("data", []):
            links = m.get("attributes", {}).get("links") or {}
            if str(links.get(key, "")).strip() == str(value).strip():
                return m["id"]
        return None

    def find_by_mal_id(self, mal_id, title=""):
        """Search MangaDex for a manga by its MAL ID. Returns MangaDex UUID or None."""
        return self.find_by_link("mal", mal_id, title)

    def find_by_al_id(self, al_id, title=""):
        """Search MangaDex for a manga by its AniList ID. Returns MangaDex UUID or None."""
        return self.find_by_link("al", al_id, title)

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

def _xml_escape(s):
    """Escape special XML characters in text content."""
    return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")

def _cdata(s):
    """Wrap text in CDATA, safely handling ]]> sequences."""
    return f'<![CDATA[{str(s).replace("]]>", "]]]]><![CDATA[>")}]]>'

def _write_xml(entries, path, uid, uname, gz=False):
    with_id = [e for e in entries if e.get("mal_id")]
    counts = defaultdict(int)
    for e in with_id: counts[e["mal_status"]] += 1
    safe_uid = _xml_escape(uid)
    safe_uname = _xml_escape(uname)
    lines = [
        '<?xml version="1.0" encoding="UTF-8" ?>',
        '<myanimelist>','<myinfo>',
        f'  <user_id>{safe_uid}</user_id>',
        f'  <user_name>{safe_uname}</user_name>',
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
            f'  <manga_title>{_cdata(e["title"])}</manga_title>',
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

def _write_kitsu_json(entries, path):
    """Write Kitsu-compatible JSON export with Kitsu IDs."""
    kitsu_entries = [e for e in entries if e.get("kitsu_id")]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(kitsu_entries, f, indent=2, ensure_ascii=False)
    return len(kitsu_entries)

def _write_mu_json(entries, path):
    """Write MangaUpdates-compatible JSON export with MU IDs."""
    mu_entries = [e for e in entries if e.get("mu_id")]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(mu_entries, f, indent=2, ensure_ascii=False)
    return len(mu_entries)

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
    all_skipped_entries = []
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
        done_ids  = set(resume_cp.get("processed_ids",[])) if resume_cp else set()
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
        global_done = sum(len(v) for s,v in all_st.items() if s in done_list)
        summary_counts = defaultdict(int)

        for status, manga_ids in all_st.items():
            if _state["stop"].is_set():
                _log("Stopped by user.", "warning"); break
            if status in done_list:
                _log(f"Skipping '{status}' (already done)", "info"); continue
            if not manga_ids: continue

            group_size = len(manga_ids)
            _log(f"── {status.upper()} ({group_size} manga) ──", "info")
            _prog(global_done/total*100 if total else 0, f"Processing {status}…")

            # Manga details
            t0 = datetime.now()
            def det_cb(done, tot, _t0=t0, _gd=global_done, _gs=group_size, _total=total):
                if not tot or not _total:
                    _prog(0, f"Fetching details {done}/{tot}"); return
                local_pct = done/tot
                overall_pct = (_gd + local_pct * _gs) / _total * 100
                elapsed = (datetime.now()-_t0).total_seconds()
                eta = f"{int((elapsed/done)*(tot-done)//60)}m {int((elapsed/done)*(tot-done)%60)}s" if done else ""
                _prog(overall_pct, f"Fetching details {done}/{tot}", eta)

            details = api.manga_details(manga_ids, det_cb)
            _log(f"✓ {len(details)} manga details fetched")

            # Deep mode chapters
            read_map = {}
            if mode == "deep":
                _log("Fetching read chapter IDs…", "info")
                deep_base = (global_done / total * 100) if total else 0
                deep_range = (group_size / total * 100) if total else 0
                _prog(deep_base + deep_range * 0.6, "Fetching read chapters…")
                ch_by_manga = api.read_chapters(manga_ids)
                all_ch = list({cid for ids in ch_by_manga.values() for cid in ids})
                _log(f"Fetching details for {len(all_ch)} chapters…")

                def ch_cb(done, total_ch, _db=deep_base, _dr=deep_range):
                    _prog(_db + _dr * (0.6 + done/total_ch*0.3) if total_ch else _db, f"Chapters {done}/{total_ch}")

                ch_det = api.chapter_details(all_ch, ch_cb)
                missing_ch = len(all_ch) - len(ch_det)
                if missing_ch > 0:
                    _log(f"⚠ {missing_ch} read chapter(s) couldn't be resolved "
                         f"(likely removed from MangaDex) — progress may read lower "
                         f"for those titles", "warning")
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
            entries, skipped, skipped_entries = [], [], []
            for mid, manga in details.items():
                attrs  = manga.get("attributes",{})
                titles = attrs.get("title",{})
                title  = (titles.get("en") or titles.get("ja-ro")
                           or next(iter(titles.values()),"Unknown"))
                links  = attrs.get("links",{}) or {}
                mal_id = links.get("mal")
                ch, vol = read_map.get(mid,(0,0)) if mode=="deep" else (0,0)
                score  = ratings.get(mid,0)
                entry = dict(manga_id=mid, title=title, status=status,
                    mal_status=MAL_MAP.get(status,"Reading"), mal_id=mal_id,
                    anilist_id=links.get("al"), kitsu_id=links.get("kt"),
                    mu_id=links.get("mu"),
                    chapter=int(ch), volume=int(vol), score=score,
                    mangadex_url=f"https://mangadex.org/title/{mid}")
                entries.append(entry)
                if not mal_id:
                    skipped.append(title)
                    skipped_entries.append(entry)
                done_ids.add(mid)

            summary_counts[status] = len(entries)
            if skipped:
                _log(f"⚠ {len(skipped)} manga have no MAL ID", "warning")
                _state["skipped"].extend(skipped)
                all_skipped_entries.extend(skipped_entries)

            if dry_run:
                _log(f"[DRY RUN] Would save {len(entries)} entries for '{status}'","warning")
                done_list.append(status)
                global_done += group_size
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

            # Kitsu JSON
            if params.get("fmt_kitsu"):
                kp = os.path.join(save_dir, f"kitsu_{status}_{ts_str}.json")
                kn = _write_kitsu_json(entries, kp)
                out.append(kp); _log(f"✓ Kitsu JSON saved: {os.path.basename(kp)} ({kn} with Kitsu ID)", "success")

            # MangaUpdates JSON
            if params.get("fmt_mu"):
                mup = os.path.join(save_dir, f"mangaupdates_{status}_{ts_str}.json")
                mun = _write_mu_json(entries, mup)
                out.append(mup); _log(f"✓ MangaUpdates JSON saved: {os.path.basename(mup)} ({mun} with MU ID)", "success")

            done_list.append(status)
            global_done += group_size
            _save_checkpoint({"completed":done_list,"processed_ids":list(done_ids),
                              "timestamp":datetime.now().isoformat()})
            _prog(global_done/total*100 if total else 100, f"✓ '{status}' done")

        if not _state["stop"].is_set():
            # Save skipped (no MAL ID) entries as JSON if any
            if all_skipped_entries and not dry_run:
                skip_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                skip_path = os.path.join(save_dir, f"mdex_no_mal_id_{skip_ts}.json")
                with open(skip_path, "w", encoding="utf-8") as sf:
                    json.dump(all_skipped_entries, sf, indent=2, ensure_ascii=False)
                exported_files.append(skip_path)
                _log(f"✓ {len(all_skipped_entries)} titles with no MAL ID saved: {os.path.basename(skip_path)}", "success")

            _clear_checkpoint()
            _state["exported"] = [f for f in exported_files if f.endswith(".xlsx")]
            elapsed = int((datetime.now()-start).total_seconds())

            # Summary log
            _log("── EXPORT SUMMARY ──", "info")
            total_exported = sum(summary_counts.values())
            for st, cnt in summary_counts.items():
                _log(f"  {st}: {cnt} manga", "info")
            _log(f"  Total exported: {total_exported} | Skipped (no MAL ID): {len(all_skipped_entries)}", "info")
            _log(f"  Files: {len(exported_files)} | Time: {elapsed}s", "info")

            _save_history({"date":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "type": target or "Full Library",
                "total": total_exported, "skipped": len(all_skipped_entries),
                "mode": mode,
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
                # Fallback: try AniList ID if no manga_id
                if not mdex_id and entry.get("anilist_id"):
                    mdex_id = api.find_by_al_id(entry["anilist_id"], title)
                    time.sleep(0.3)
            else:
                # XML — need to look up MangaDex UUID from MAL ID
                mal_id = entry.get("mal_id")
                mal_status = entry.get("status", "Reading")
                score = entry.get("score", 0)
                mdex_status = MAL_REVERSE.get(mal_status, "reading")

                _log(f"Looking up '{title}' (MAL #{mal_id})…", "info")
                mdex_id = api.find_by_mal_id(mal_id, title)
                if not mdex_id:
                    # Try AniList ID as fallback
                    al_id = entry.get("anilist_id")
                    if al_id:
                        _log(f"  MAL lookup failed, trying AniList #{al_id}…", "info")
                        mdex_id = api.find_by_al_id(al_id, title)
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
        try:
            while True:
                try:
                    msg = _state["log_queue"].get(timeout=0.5)
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    yield f"data: {json.dumps({'ping':1})}\n\n"
        except GeneratorExit:
            pass  # Client disconnected — clean up
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

@app.route("/api/test_credentials", methods=["POST"])
def test_credentials():
    """Real credential verification (backs the 'Verified' badge on the
    Credentials card). Returns how many seconds the resulting token is
    actually good for, straight from API._store()'s real expires_at."""
    p = request.json or {}
    if not all(p.get(k) for k in ("client_id", "client_secret", "username", "password")):
        return jsonify(ok=False, error="Missing credentials"), 400
    api = API()
    if not api.auth(p["client_id"], p["client_secret"], p["username"], p["password"]):
        return jsonify(ok=False, error="Authentication failed"), 401
    expires_in = int((api.expires_at - datetime.now()).total_seconds())
    return jsonify(ok=True, expires_in=max(0, expires_in))

# External links the UI may ask the OS to open (pywebview can't follow target=_blank itself).
_OPEN_URL_PREFIXES = ("https://myanimelist.net/", "https://mangadex.org/", "https://anilist.co/")

@app.route("/api/open_url", methods=["POST"])
def open_url():
    url = (request.json or {}).get("url", "")
    if not url.startswith(_OPEN_URL_PREFIXES):
        return jsonify(ok=False, error="URL not allowed"), 400
    webbrowser.open(url)
    return jsonify(ok=True)

@app.route("/api/info")
def info():
    import platform
    return jsonify(version=APP_VERSION, cwd=os.getcwd(),
                   history_file=os.path.abspath(_history_file),
                   checkpoint_file=os.path.abspath(_checkpoint_file),
                   platform=f"{platform.system()} {platform.release()}",
                   python=platform.python_version())

@app.route("/api/library_counts", methods=["POST"])
def library_counts():
    """Real per-status title counts for the export status chips. A quick
    auth + single /manga/status call, independent of the export worker so
    it can't collide with a run in progress."""
    if _state["running"]:
        return jsonify(ok=False, error="Export already running"), 400
    p = request.json or {}
    if not all(p.get(k) for k in ("client_id", "client_secret", "username", "password")):
        return jsonify(ok=False, error="Missing credentials"), 400
    api = API()
    if not api.auth(p["client_id"], p["client_secret"], p["username"], p["password"]):
        return jsonify(ok=False, error="Authentication failed"), 401
    all_st = api.statuses()
    counts = {s: len(all_st.get(s, [])) for s in STATUSES}
    return jsonify(ok=True, counts=counts, total=sum(counts.values()))

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

@app.route("/api/history/clear", methods=["POST"])
def clear_history():
    try: os.remove(_history_file)
    except Exception: pass
    return jsonify(ok=True)

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

def _native_linux_dialog(args, timeout=120):
    """Run a native Linux dialog command (zenity/kdialog) and return its
    stdout path, or None if the tool isn't available / failed to launch
    (as opposed to the user simply cancelling, which is a clean "" result).
    """
    import subprocess
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        return None
    # returncode 0 = a path was chosen, 1 = the user cancelled (both are a
    # real answer from a dialog that did launch); anything else means the
    # tool itself errored out, so fall back instead of trusting its output.
    if result.returncode in (0, 1):
        return result.stdout.strip()
    return None

def _pick_folder():
    """Native folder picker matching the desktop's own file manager.
    Tk's picker isn't native-themed on Linux (unlike Windows/macOS, where
    tkinter already calls the OS's own dialog), so prefer zenity (GTK/
    GNOME/Nautilus look) or kdialog (KDE/Dolphin look) there, falling back
    to Tk only if neither is installed."""
    import platform, shutil
    if platform.system() == "Linux":
        if shutil.which("zenity"):
            path = _native_linux_dialog(
                ["zenity", "--file-selection", "--directory", "--title=Choose save folder"])
            if path is not None:
                return path
        elif shutil.which("kdialog"):
            path = _native_linux_dialog(
                ["kdialog", "--getexistingdirectory", os.getcwd(), "--title", "Choose save folder"])
            if path is not None:
                return path
    return _run_tk_subprocess(_TK_BROWSE_FOLDER)

def _pick_file():
    """Native file picker; see _pick_folder() for why Linux needs zenity/kdialog."""
    import platform, shutil
    if platform.system() == "Linux":
        if shutil.which("zenity"):
            path = _native_linux_dialog(
                ["zenity", "--file-selection", "--title=Select import file",
                 "--file-filter=XML / JSON files | *.xml *.json",
                 "--file-filter=All files | *"])
            if path is not None:
                return path
        elif shutil.which("kdialog"):
            path = _native_linux_dialog(
                ["kdialog", "--getopenfilename", os.getcwd(),
                 "*.xml *.json|XML / JSON files", "--title", "Select import file"])
            if path is not None:
                return path
    return _run_tk_subprocess(_TK_BROWSE_FILE)

@app.route("/api/browse_folder")
def browse_folder():
    path = _pick_folder()
    return jsonify(ok=bool(path), path=path)

@app.route("/api/browse_file")
def browse_file():
    path = _pick_file()
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
<title>MangaDex Sync</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:opsz,wght@9..40,400;9..40,500;9..40,600;9..40,700&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0b0d12;  --panel: #12151c;  --card: #161a23;  --card-2: #1c212c;  --field: #0e1117;
  --border: #252a36;  --border-2: #323949;  --divider: #1f2430;
  --text: #eef0f6;  --text-2: #bcc2d0;  --muted: #8b93a7;  --faint: #5b6272;
  --accent: #e8823c;  --accent-2: #f2954f;  --accent-ink: #1a0f06;
  --accent-tint: rgba(232,130,60,.10);  --accent-line: rgba(232,130,60,.38);
  --ok: #3ecf8e;  --ok-tint: rgba(62,207,142,.10);  --ok-line: rgba(62,207,142,.35);
  --warn: #e0b23e;  --warn-tint: rgba(224,178,62,.10);  --warn-line: rgba(224,178,62,.35);
  --err: #ef5350;  --err-tint: rgba(239,83,80,.10);  --err-line: rgba(239,83,80,.35);
  --info: #5fa8e8; --info-tint: rgba(95,168,232,.10); --info-line: rgba(95,168,232,.32);

  --c-reading: #5fa8e8;      --t-reading: rgba(95,168,232,.12);
  --c-completed: #3ecfc2;    --t-completed: rgba(62,207,194,.12);
  --c-on_hold: #dc9a45;      --t-on_hold: rgba(220,154,69,.12);
  --c-dropped: #e8768a;      --t-dropped: rgba(232,118,138,.12);
  --c-plan_to_read: #9a94f0; --t-plan_to_read: rgba(154,148,240,.12);
  --c-re_reading: #c98adb;   --t-re_reading: rgba(201,138,219,.12);

  --sans: 'DM Sans', ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;
  --mono: 'Space Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace;
  --r-xs: 4px; --r-sm: 6px; --r-md: 8px;
  --ctl: 32px;
  --ring: 0 0 0 1px var(--bg), 0 0 0 3px var(--accent-line);
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
[hidden] { display: none !important; }
html { color-scheme: dark; }
body { background: var(--bg); color: var(--text); font: 13px/1.5 var(--sans); display: flex;
       height: 100vh; overflow: hidden; -webkit-font-smoothing: antialiased; }
::selection { background: var(--accent); color: var(--accent-ink); }
button, input, select { font: inherit; color: inherit; }
button { cursor: pointer; background: none; border: 0; }
:focus { outline: none; }
:focus-visible { box-shadow: var(--ring); }
.num { font-family: var(--mono); font-variant-numeric: tabular-nums; }

.i { width: 1em; height: 1em; stroke: currentColor; stroke-width: 1.75; fill: none; stroke-linecap: round;
     stroke-linejoin: round; flex-shrink: 0; }
.i .dot { fill: currentColor; stroke: none; }

/* ── Sidebar ── */
.sidebar { width: 232px; flex-shrink: 0; background: var(--panel); border-right: 1px solid var(--border);
           display: flex; flex-direction: column; }
.brand { display: flex; align-items: center; gap: 10px; height: 56px; padding: 0 16px; border-bottom: 1px solid var(--divider); }
.brand-mark { width: 28px; height: 28px; border-radius: var(--r-sm); display: grid; place-items: center;
              background: linear-gradient(150deg, var(--accent-2), var(--accent)); color: var(--accent-ink);
              font-size: 15px; box-shadow: 0 4px 14px -6px rgba(232,130,60,.8), inset 0 1px 0 rgba(255,255,255,.25); }
.brand-name { font-family: var(--mono); font-size: 13px; font-weight: 700; letter-spacing: .01em; line-height: 1.2; }
.brand-name b { color: var(--accent); font-size: 10px; letter-spacing: .12em; margin-left: 4px; }
.brand-ver { display: block; font-family: var(--mono); font-size: 10px; color: var(--faint); }
.nav-label { font-family: var(--mono); font-size: 10px; font-weight: 700; letter-spacing: .12em;
             text-transform: uppercase; color: var(--faint); padding: 18px 16px 8px; }
nav { display: flex; flex-direction: column; gap: 2px; padding: 0 8px; }
.nav-item { position: relative; display: flex; align-items: center; gap: 10px; height: 34px; padding: 0 10px;
            border-radius: var(--r-sm); color: var(--muted); font-size: 13px; font-weight: 500; text-align: left;
            transition: background-color .15s, color .15s; }
.nav-item .i { font-size: 16px; }
.nav-item kbd { margin-left: auto; }
.nav-item:hover { background: var(--card); color: var(--text); }
.nav-item.active { background: var(--card); color: var(--text); box-shadow: inset 0 0 0 1px var(--border); }
.nav-item.active .i { color: var(--accent); }
.nav-item.active::before { content: ""; position: absolute; left: -8px; top: 9px; bottom: 9px; width: 3px;
                           border-radius: 0 3px 3px 0; background: var(--accent); }
.side-foot { margin-top: auto; border-top: 1px solid var(--divider); padding: 12px 16px 14px; display: grid; gap: 7px; }
.kv { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-family: var(--mono); font-size: 10.5px; }
.kv > span:first-child { color: var(--faint); letter-spacing: .08em; text-transform: uppercase; }
.kv > span:last-child { color: var(--text-2); display: inline-flex; align-items: center; gap: 6px; white-space: nowrap;
                         overflow: hidden; text-overflow: ellipsis; }
.dot { width: 6px; height: 6px; border-radius: 50%; background: var(--faint); flex-shrink: 0; display: inline-block; }
.dot.ok { background: var(--ok); } .dot.accent { background: var(--accent); } .dot.warn { background: var(--warn); }
.dot.live { background: var(--accent); animation: pulse 1.2s ease-in-out infinite; }
@keyframes pulse { 0%,100% { opacity: 1 } 50% { opacity: .3 } }

/* ── Main ── */
.main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.topbar { height: 56px; flex-shrink: 0; background: var(--panel); border-bottom: 1px solid var(--border);
          display: flex; align-items: center; gap: 12px; padding: 0 20px; }
.crumbs { font-family: var(--mono); font-size: 11px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase;
          display: flex; align-items: center; gap: 8px; color: var(--faint); }
.crumbs .sep { color: var(--border-2); }
.crumbs #pageTitle { color: var(--text); }
.top-right { margin-left: auto; display: flex; align-items: center; gap: 8px; }
.run-meter { display: flex; align-items: center; gap: 10px; height: 30px; padding: 0 10px; border: 1px solid var(--border);
             border-radius: var(--r-sm); background: var(--field); font-family: var(--mono); font-size: 11px; }
.run-meter .pct { color: var(--accent); font-weight: 700; min-width: 4ch; }
.run-meter .mini { width: 96px; height: 4px; border-radius: 2px; background: var(--border); overflow: hidden; }
.run-meter .mini > i { display: block; height: 100%; width: 100%; background: var(--accent); transform: scaleX(0);
                       transform-origin: left; transition: transform .4s ease; }
.run-meter .eta { color: var(--muted); }

.content { flex: 1; overflow-y: auto; padding: 20px 24px 28px; }
.page { display: none; max-width: 1360px; margin: 0 auto; }
.page.active { display: block; }
.page-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
.page-head h1 { font-family: var(--mono); font-size: 20px; font-weight: 700; letter-spacing: -.01em; line-height: 1.2; }
.page-head p { color: var(--muted); font-size: 13px; margin-top: 4px; max-width: 70ch; }
.head-meta { display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }

.grid-2 { display: grid; grid-template-columns: minmax(0,1fr) minmax(0,1fr); gap: 14px; align-items: start; }
.col { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
@media (max-width: 1120px) { .grid-2 { grid-template-columns: minmax(0,1fr); } }

/* ── Card ── */
.card { background: var(--card); border: 1px solid var(--border); border-radius: var(--r-md); min-width: 0; }
.card-hd { display: flex; align-items: center; justify-content: space-between; gap: 10px; min-height: 42px;
           padding: 0 14px; border-bottom: 1px solid var(--divider); }
.card-t { display: flex; align-items: center; gap: 8px; font-family: var(--mono); font-size: 11.5px; font-weight: 700;
          letter-spacing: .08em; text-transform: uppercase; white-space: nowrap; }
.card-t .step { color: var(--accent); }
.card-t .i { color: var(--accent); font-size: 14px; }
.card-tools { display: flex; align-items: center; gap: 6px; }
.card-bd { padding: 14px; }
.card-bd.flush { padding: 0; }

/* ── Pills ── */
.pill { display: inline-flex; align-items: center; gap: 6px; height: 20px; padding: 0 7px; border-radius: var(--r-xs);
        border: 1px solid var(--border-2); color: var(--muted); font-family: var(--mono); font-size: 10px; font-weight: 700;
        letter-spacing: .07em; text-transform: uppercase; white-space: nowrap; }
.pill.ok { color: var(--ok); border-color: var(--ok-line); background: var(--ok-tint); }
.pill.warn { color: var(--warn); border-color: var(--warn-line); background: var(--warn-tint); }
.pill.err { color: var(--err); border-color: var(--err-line); background: var(--err-tint); }
.pill.accent { color: var(--accent); border-color: var(--accent-line); background: var(--accent-tint); }
.pill.info { color: var(--info); border-color: var(--info-line); background: var(--info-tint); }
.pill .val { color: var(--text); }
.sub-note { font-family: var(--mono); font-size: 10.5px; color: var(--faint); }

/* ── Fields ── */
.field + .field, .row-2 + .field, .field + .row-2 { margin-top: 12px; }
.row-2 { display: grid; grid-template-columns: minmax(0,1fr) minmax(0,1fr); gap: 10px; }
.lbl { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; margin-bottom: 6px; }
.lbl label, .lbl .l { font-family: var(--mono); font-size: 10.5px; font-weight: 700; letter-spacing: .08em;
                      text-transform: uppercase; color: var(--muted); }
.lbl .h { font-size: 11px; color: var(--faint); text-align: right; }
.control { display: flex; align-items: stretch; height: var(--ctl); background: var(--field); border: 1px solid var(--border);
           border-radius: var(--r-sm); overflow: hidden; transition: border-color .15s, box-shadow .15s; }
.control:hover { border-color: var(--border-2); }
.control:focus-within { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-tint); }
.control .pre { display: grid; place-items: center; padding-left: 10px; color: var(--accent); font-family: var(--mono);
                font-size: 12px; font-weight: 700; }
.control .pre .i { color: var(--faint); font-size: 14px; }
.control input { flex: 1; min-width: 0; background: transparent; border: 0; outline: none; padding: 0 10px; font-size: 13px; }
.control input.mono { font-family: var(--mono); font-size: 12px; }
.control input::placeholder { color: var(--faint); }
.control input:focus-visible { box-shadow: none; }
.ctl-btn { display: inline-flex; align-items: center; gap: 6px; padding: 0 10px; border-left: 1px solid var(--divider);
           color: var(--muted); font-family: var(--mono); font-size: 10.5px; font-weight: 700; letter-spacing: .07em;
           text-transform: uppercase; transition: color .15s, background-color .15s; }
.ctl-btn:hover { color: var(--text); background: var(--card-2); }
.ctl-btn .i { font-size: 13px; }
.ctl-btn.icon { padding: 0 9px; }
.ctl-btn:focus-visible { box-shadow: inset 0 0 0 2px var(--accent-line); }

/* ── Buttons ── */
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 7px; height: var(--ctl); padding: 0 12px;
       border-radius: var(--r-sm); border: 1px solid var(--border-2); color: var(--text); font-family: var(--mono);
       font-size: 11px; font-weight: 700; letter-spacing: .07em; text-transform: uppercase; white-space: nowrap;
       transition: background-color .15s, border-color .15s, color .15s, transform .08s; }
.btn .i { font-size: 14px; }
.btn:hover:not(:disabled) { background: var(--card-2); border-color: #3e4559; }
.btn:active:not(:disabled) { transform: translateY(1px); }
.btn:disabled { opacity: .38; cursor: not-allowed; }
.btn-sm { height: 26px; padding: 0 9px; font-size: 10.5px; }
.btn-sm .i { font-size: 12px; }
.btn-ghost { border-color: transparent; color: var(--muted); }
.btn-ghost:hover:not(:disabled) { color: var(--text); }
.btn-primary { background: var(--accent); border-color: var(--accent); color: var(--accent-ink);
               box-shadow: inset 0 1px 0 rgba(255,255,255,.22), 0 6px 18px -8px rgba(232,130,60,.7); }
.btn-primary:hover:not(:disabled) { background: var(--accent-2); border-color: var(--accent-2); }
.btn-lg { height: 42px; font-size: 12px; padding: 0 16px; }
.btn-lg .i { font-size: 16px; }
.btn-danger { background: var(--err-tint); border-color: var(--err-line); color: var(--err); }
.btn-danger:hover:not(:disabled) { background: rgba(239,83,80,.2); border-color: var(--err); }
.btn-ok { background: var(--ok); border-color: var(--ok); color: #05261a;
          box-shadow: inset 0 1px 0 rgba(255,255,255,.22), 0 6px 18px -8px rgba(62,207,142,.6); }
.btn-ok:hover:not(:disabled) { filter: brightness(1.08); }
.btn .count { opacity: .7; font-weight: 400; }
.action-row { display: flex; gap: 8px; }
.action-row .grow { flex: 1; }

/* ── Segmented ── */
.seg { display: grid; grid-template-columns: 1fr 1fr; gap: 3px; padding: 3px; background: var(--field);
       border: 1px solid var(--border); border-radius: var(--r-sm); }
.seg-opt { text-align: left; padding: 8px 10px; border-radius: var(--r-xs); transition: background-color .15s; }
.seg-opt:hover { background: var(--card); }
.seg-opt .t { display: flex; align-items: center; gap: 7px; font-family: var(--mono); font-size: 11.5px; font-weight: 700;
              letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }
.seg-opt .t .i { font-size: 13px; }
.seg-opt .s { display: block; margin-top: 3px; font-size: 12px; color: var(--faint); line-height: 1.4; }
.seg-opt.active { background: var(--accent-tint); box-shadow: inset 0 0 0 1px var(--accent-line); }
.seg-opt.active .t { color: var(--accent); }
.seg-opt.active .s { color: var(--text-2); }

.tabs { display: inline-flex; gap: 2px; padding: 2px; background: var(--field); border: 1px solid var(--border); border-radius: var(--r-sm); }
.tab { height: 26px; padding: 0 10px; border-radius: var(--r-xs); font-family: var(--mono); font-size: 10.5px; font-weight: 700;
       letter-spacing: .07em; text-transform: uppercase; color: var(--muted); }
.tab:hover { color: var(--text); }
.tab.active { background: var(--card-2); color: var(--text); box-shadow: inset 0 0 0 1px var(--border-2); }
.tab .n { color: var(--faint); margin-left: 4px; }

/* ── Option tiles (checkbox) ── */
.opts { display: grid; grid-template-columns: minmax(0,1fr) minmax(0,1fr); gap: 8px; }
.opts.one { grid-template-columns: minmax(0,1fr); }
.opt { display: flex; gap: 10px; align-items: flex-start; padding: 10px; background: var(--field); border: 1px solid var(--border);
       border-radius: var(--r-sm); cursor: pointer; transition: border-color .15s, background-color .15s; }
.opt:hover { border-color: var(--border-2); }
.opt:has(input:checked) { border-color: var(--accent-line); background: rgba(232,130,60,.05); }
.opt input { appearance: none; -webkit-appearance: none; width: 16px; height: 16px; margin-top: 1px; flex-shrink: 0;
             border: 1px solid var(--border-2); border-radius: var(--r-xs); background: var(--panel); cursor: pointer;
             transition: background-color .15s, border-color .15s; }
.opt input:checked { background: var(--accent) url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><path d='M3.5 8.5l3 3 6-7' fill='none' stroke='%231a0f06' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'/></svg>") center/12px no-repeat;
                     border-color: var(--accent); }
.opt input:focus-visible { box-shadow: var(--ring); }
.opt .ot { display: block; font-size: 13px; font-weight: 600; color: var(--text); line-height: 1.3; }
.opt .os { display: block; font-size: 12px; color: var(--faint); margin-top: 2px; line-height: 1.35; }

/* ── Status tiles ── */
.st-grid { display: grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: 6px; }
.st { display: flex; align-items: center; justify-content: space-between; gap: 8px; height: 36px; padding: 0 10px;
      border-radius: var(--r-xs); border: 1px solid; font-family: var(--mono); font-size: 10.5px; font-weight: 700;
      letter-spacing: .06em; text-transform: uppercase; transition: filter .15s, transform .08s; }
.st:hover:not(:disabled) { filter: brightness(1.2); }
.st:active:not(:disabled) { transform: translateY(1px); }
.st:disabled { opacity: .4; cursor: not-allowed; }
.st .cnt { font-size: 12px; font-variant-numeric: tabular-nums; color: var(--text); }
.st .go { font-size: 13px; opacity: .6; }
.st[data-status="reading"]      { color: var(--c-reading);      border-color: rgba(95,168,232,.45);  background: var(--t-reading); }
.st[data-status="completed"]    { color: var(--c-completed);    border-color: rgba(62,207,194,.45);  background: var(--t-completed); }
.st[data-status="on_hold"]      { color: var(--c-on_hold);      border-color: rgba(220,154,69,.45);  background: var(--t-on_hold); }
.st[data-status="dropped"]      { color: var(--c-dropped);      border-color: rgba(232,118,138,.45); background: var(--t-dropped); }
.st[data-status="plan_to_read"] { color: var(--c-plan_to_read); border-color: rgba(154,148,240,.45); background: var(--t-plan_to_read); }
.st[data-status="re_reading"]   { color: var(--c-re_reading);   border-color: rgba(201,138,219,.45); background: var(--t-re_reading); }
.sect-lbl { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px;
            font-family: var(--mono); font-size: 10.5px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }
.sect-lbl .h { font-weight: 400; color: var(--faint); letter-spacing: .04em; }
.divider { height: 1px; background: var(--divider); margin: 14px -14px; }

/* ── Progress ── */
.prog-top { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.prog-pct { font-family: var(--mono); font-size: 30px; font-weight: 700; line-height: 1; font-variant-numeric: tabular-nums; }
.prog-pct small { font-size: 14px; color: var(--muted); margin-left: 2px; }
.prog-pct.idle { color: var(--faint); }
.prog-label { font-size: 12.5px; color: var(--text-2); text-align: right; max-width: 60%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.track { position: relative; height: 8px; border-radius: var(--r-xs); background: var(--field); border: 1px solid var(--border); overflow: hidden; }
.track::after { content: ""; position: absolute; inset: 0; pointer-events: none;
                background: repeating-linear-gradient(90deg, transparent 0 calc(5% - 1px), rgba(11,13,18,.9) calc(5% - 1px) 5%); }
.track .fill { height: 100%; width: 100%; background: linear-gradient(90deg, var(--accent), var(--accent-2));
               transform: scaleX(0); transform-origin: left; transition: transform .45s ease; }
.prog-meta { display: grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: 10px; margin-top: 12px; padding-top: 12px; border-top: 1px dashed var(--divider); }
.prog-meta div { min-width: 0; }
.prog-meta .k { display: block; font-family: var(--mono); font-size: 10px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: var(--faint); }
.prog-meta .v { display: block; font-family: var(--mono); font-size: 12px; color: var(--text-2); margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

/* ── Log ── */
.log-wrap { position: relative; }
.log-box { height: 300px; overflow-y: auto; background: #08090d; font-family: var(--mono); font-size: 11.5px; line-height: 1.8;
           padding: 10px 14px; border-radius: 0 0 var(--r-md) var(--r-md); }
.log-line { display: flex; gap: 8px; white-space: pre-wrap; word-break: break-word; }
.log-line .ts { color: var(--faint); flex-shrink: 0; }
.log-line .tg { font-weight: 700; flex-shrink: 0; min-width: 6ch; }
.log-line .m { color: var(--text-2); }
.log-line.info .tg { color: var(--info); }
.log-line.success .tg, .log-line.success .m { color: var(--ok); }
.log-line.warning .tg, .log-line.warning .m { color: var(--warn); }
.log-line.error .tg, .log-line.error .m { color: var(--err); }
.log-sect { display: flex; align-items: center; gap: 10px; margin: 6px 0 2px; color: var(--accent); font-weight: 700;
            letter-spacing: .08em; text-transform: uppercase; font-size: 10.5px; }
.log-sect::before, .log-sect::after { content: ""; height: 1px; background: var(--divider); flex: 1; }
.log-sect::before { flex: 0 0 12px; }
.log-empty { position: absolute; inset: 0; display: grid; place-content: center; justify-items: center; gap: 6px; text-align: center; pointer-events: none; }
.log-empty .i { font-size: 22px; color: var(--faint); margin-bottom: 4px; }
.log-empty .t { font-family: var(--mono); font-size: 11px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase; color: var(--muted); }
.log-empty .s { font-size: 12px; color: var(--faint); max-width: 34ch; }
.log-empty .caret { display: inline-block; width: 7px; height: 13px; background: var(--accent); vertical-align: -2px; margin-left: 4px; animation: blink 1.1s steps(1) infinite; }
@keyframes blink { 50% { opacity: 0 } }
.toggle-pill { cursor: pointer; }
.toggle-pill:hover { border-color: var(--muted); }

/* ── Rows / lists ── */
.row-list { max-height: 260px; overflow-y: auto; }
.row { display: flex; align-items: center; gap: 10px; padding: 8px 14px; border-bottom: 1px solid var(--divider); min-height: 40px; }
.row:last-child { border-bottom: 0; }
.row:hover { background: var(--card-2); }
.row .idx { font-family: var(--mono); font-size: 10.5px; color: var(--faint); min-width: 3ch; }
.row .ttl { flex: 1; min-width: 0; font-size: 13px; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.row .ttl.mono { font-family: var(--mono); font-size: 12px; }
.row .i.lead { color: var(--muted); font-size: 15px; }
.row select { height: 26px; background: var(--field); border: 1px solid var(--border); border-radius: var(--r-xs);
              font-family: var(--mono); font-size: 11px; padding: 0 6px; cursor: pointer; }
.row .acts { display: flex; gap: 2px; opacity: .75; }
.row:hover .acts { opacity: 1; }

.empty { display: grid; justify-items: center; gap: 6px; padding: 30px 16px; text-align: center; }
.empty > .i { font-size: 22px; color: var(--faint); margin-bottom: 4px; }
.empty .t { font-family: var(--mono); font-size: 11px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase; color: var(--muted); }
.empty .s { font-size: 12.5px; color: var(--faint); max-width: 42ch; }
.empty .action-row { margin-top: 8px; }

/* ── Callout ── */
.callout { display: flex; gap: 10px; padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--r-sm); background: var(--field); }
.callout .i { font-size: 15px; margin-top: 1px; }
.callout .ct { display: block; font-family: var(--mono); font-size: 10.5px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
.callout .cs { display: block; font-size: 12.5px; color: var(--text-2); margin-top: 2px; }
.callout.warn { border-color: var(--warn-line); background: var(--warn-tint); } .callout.warn .i, .callout.warn .ct { color: var(--warn); }
.callout.ok { border-color: var(--ok-line); background: var(--ok-tint); } .callout.ok .i, .callout.ok .ct { color: var(--ok); }
.callout.info { border-color: var(--info-line); background: var(--info-tint); } .callout.info .i, .callout.info .ct { color: var(--info); }
.callout.err { border-color: var(--err-line); background: var(--err-tint); } .callout.err .i, .callout.err .ct { color: var(--err); }

/* ── kbd / hints ── */
kbd { display: inline-grid; place-items: center; min-width: 18px; height: 18px; padding: 0 5px; font-family: var(--mono);
      font-size: 10px; color: var(--muted); background: var(--field); border: 1px solid var(--border-2); border-bottom-width: 2px;
      border-radius: var(--r-xs); line-height: 1; }
.hints { display: flex; flex-wrap: wrap; gap: 6px 16px; margin-top: 12px; font-family: var(--mono); font-size: 10.5px;
         letter-spacing: .05em; text-transform: uppercase; color: var(--faint); }
.hints span { display: inline-flex; align-items: center; gap: 5px; }

/* ── Stats / table ── */
.stats { display: grid; grid-template-columns: repeat(4, minmax(0,1fr)); gap: 10px; margin-bottom: 14px; }
@media (max-width: 1000px) { .stats { grid-template-columns: repeat(2, minmax(0,1fr)); } }
.stat { background: var(--card); border: 1px solid var(--border); border-radius: var(--r-md); padding: 12px 14px; }
.stat-hd { display: flex; justify-content: space-between; align-items: center; font-family: var(--mono); font-size: 10px; font-weight: 700;
           letter-spacing: .1em; text-transform: uppercase; color: var(--muted); }
.stat-hd .i { color: var(--faint); font-size: 14px; }
.stat-v { margin-top: 10px; font-family: var(--mono); font-size: 26px; font-weight: 700; line-height: 1; font-variant-numeric: tabular-nums; }
.stat-v small { font-family: var(--sans); font-size: 12px; font-weight: 500; color: var(--muted); margin-left: 1px; }
.stat-s { margin-top: 8px; font-family: var(--mono); font-size: 10.5px; color: var(--faint); }
.stat.ok .stat-v { color: var(--ok); }
.filterbar { display: flex; gap: 8px; align-items: center; padding: 10px 14px; border-bottom: 1px solid var(--divider); flex-wrap: wrap; }
.filterbar .control { flex: 1; min-width: 220px; }
.tbl-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
th { height: 34px; padding: 0 14px; text-align: left; font-family: var(--mono); font-size: 10px; font-weight: 700; letter-spacing: .09em;
     text-transform: uppercase; color: var(--muted); background: var(--panel); border-bottom: 1px solid var(--divider); white-space: nowrap; }
td { height: 40px; padding: 0 14px; border-bottom: 1px solid var(--divider); color: var(--text-2); white-space: nowrap; }
tr:last-child td { border-bottom: 0; }
tbody tr:hover td { background: var(--card-2); }
th.r, td.r { text-align: right; }
td.num, td .num { color: var(--text); }
td.files { font-family: var(--mono); font-size: 11px; color: var(--faint); max-width: 320px; overflow: hidden; text-overflow: ellipsis; }
.tbl-foot { display: flex; justify-content: space-between; align-items: center; padding: 10px 14px; border-top: 1px solid var(--divider);
            font-family: var(--mono); font-size: 10.5px; color: var(--faint); letter-spacing: .04em; }

/* ── Settings list ── */
.kv-list { display: grid; }
.kv-row { display: grid; grid-template-columns: 150px minmax(0,1fr); gap: 12px; padding: 9px 14px; border-bottom: 1px solid var(--divider); align-items: center; }
.kv-row:last-child { border-bottom: 0; }
.kv-row .k { font-family: var(--mono); font-size: 10.5px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: var(--faint); }
.kv-row .v { font-family: var(--mono); font-size: 12px; color: var(--text-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.kv-row .v.wrap { white-space: normal; word-break: break-all; }

/* ── Toast ── */
#toast { position: fixed; right: 20px; bottom: 20px; z-index: 50; display: flex; align-items: center; gap: 10px; max-width: 380px;
         padding: 10px 14px; background: var(--card-2); border: 1px solid var(--border-2); border-radius: var(--r-md);
         box-shadow: 0 10px 30px -8px rgba(0,0,0,.7); font-size: 13px; transform: translateY(12px); opacity: 0;
         pointer-events: none; transition: transform .2s ease, opacity .2s ease; }
#toast.show { transform: none; opacity: 1; }
#toast .i { font-size: 16px; }
#toast.ok .i { color: var(--ok); } #toast.err .i { color: var(--err); } #toast.warn .i { color: var(--warn); }

::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; border: 2px solid transparent; background-clip: padding-box; }
::-webkit-scrollbar-thumb:hover { background: var(--border-2); background-clip: padding-box; }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: .001ms !important; animation-iteration-count: 1 !important; transition-duration: .001ms !important; }
}
</style>
</head>
<body>

<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <symbol id="i-upload" viewBox="0 0 24 24"><path d="M12 15V4M8 8l4-4 4 4"/><path d="M4 15v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/></symbol>
  <symbol id="i-download" viewBox="0 0 24 24"><path d="M12 4v11M8 11l4 4 4-4"/><path d="M4 15v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/></symbol>
  <symbol id="i-convert" viewBox="0 0 24 24"><path d="M4 8h13l-3-3M20 16H7l3 3"/></symbol>
  <symbol id="i-refresh" viewBox="0 0 24 24"><path d="M4 10a8 8 0 0 1 14-5.3M20 4v5h-5"/><path d="M20 14a8 8 0 0 1-14 5.3M4 20v-5h5"/></symbol>
  <symbol id="i-history" viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3 2"/></symbol>
  <symbol id="i-settings" viewBox="0 0 24 24"><path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/></symbol>
  <symbol id="i-bolt" viewBox="0 0 24 24"><path d="M13 2 4 14h6l-1 8 9-12h-6l1-8z"/></symbol>
  <symbol id="i-chart" viewBox="0 0 24 24"><path d="M4 20V10M10 20V4M16 20v-7M3 20h18"/></symbol>
  <symbol id="i-log" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M13 15h4"/></symbol>
  <symbol id="i-folder" viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/></symbol>
  <symbol id="i-warning" viewBox="0 0 24 24"><path d="M12 3l10 18H2L12 3z"/><path d="M12 10v4"/><path d="M12 17.2v.01"/></symbol>
  <symbol id="i-info" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 7.8v.01"/></symbol>
  <symbol id="i-check" viewBox="0 0 24 24"><path d="M4 12l5 5L20 6"/></symbol>
  <symbol id="i-check-circle" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9.5"/></symbol>
  <symbol id="i-x" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></symbol>
  <symbol id="i-trash" viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/><path d="M10 11v6M14 11v6"/></symbol>
  <symbol id="i-plus" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></symbol>
  <symbol id="i-play" viewBox="0 0 24 24"><path d="M7 4.5l12 7.5-12 7.5v-15z"/></symbol>
  <symbol id="i-stop" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="1.5"/></symbol>
  <symbol id="i-clipboard" viewBox="0 0 24 24"><rect x="6" y="4" width="12" height="17" rx="2"/><path d="M9 4V3h6v1"/></symbol>
  <symbol id="i-file" viewBox="0 0 24 24"><path d="M6 3h9l5 5v13H6V3z"/><path d="M14 3v5h5"/></symbol>
  <symbol id="i-sheet" viewBox="0 0 24 24"><rect x="4" y="3" width="16" height="18" rx="2"/><path d="M4 9h16M4 15h16M10 9v12"/></symbol>
  <symbol id="i-braces" viewBox="0 0 24 24"><path d="M8 4c-2 0-3 1-3 3v2c0 1.5-1 2.5-2 3 1 .5 2 1.5 2 3v2c0 2 1 3 3 3M16 4c2 0 3 1 3 3v2c0 1.5 1 2.5 2 3-1 .5-2 1.5-2 3v2c0 2-1 3-3 3"/></symbol>
  <symbol id="i-code" viewBox="0 0 24 24"><path d="M8 7l-5 5 5 5M16 7l5 5-5 5"/></symbol>
  <symbol id="i-eye" viewBox="0 0 24 24"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></symbol>
  <symbol id="i-eye-off" viewBox="0 0 24 24"><path d="M3 3l18 18"/><path d="M10.6 5.1A10.4 10.4 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-3.2 4.1M6.6 6.6C3.8 8.4 2 12 2 12s3.5 7 10 7a9.7 9.7 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/></symbol>
  <symbol id="i-copy" viewBox="0 0 24 24"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/></symbol>
  <symbol id="i-external" viewBox="0 0 24 24"><path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></symbol>
  <symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/></symbol>
  <symbol id="i-arrow" viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></symbol>
  <symbol id="i-shield" viewBox="0 0 24 24"><path d="M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6l8-3z"/><path d="M8.5 12l2.5 2.5 4.5-5"/></symbol>
  <symbol id="i-keyboard" viewBox="0 0 24 24"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/></symbol>
  <symbol id="i-layers" viewBox="0 0 24 24"><path d="M12 3l9 5-9 5-9-5 9-5z"/><path d="M3 13l9 5 9-5"/></symbol>
  <symbol id="i-bookmark" viewBox="0 0 24 24"><path d="M6 3h12v18l-6-4-6 4V3z"/></symbol>
  <symbol id="i-user" viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 4-6 8-6s8 2 8 6"/></symbol>
  <symbol id="i-key" viewBox="0 0 24 24"><circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M17 6l3 3M14 9l2 2"/></symbol>
  <symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></symbol>
</svg>

<aside class="sidebar">
  <div class="brand">
    <span class="brand-mark"><svg class="i"><use href="#i-bolt"/></svg></span>
    <div>
      <div class="brand-name">MangaDex<b>SYNC</b></div>
      <span class="brand-ver" id="brandVer">v2.2.1</span>
    </div>
  </div>
  <div class="nav-label">Workspace</div>
  <nav aria-label="Main">
    <button class="nav-item active" data-page="export" data-label="Export"><svg class="i"><use href="#i-upload"/></svg>Export<kbd>1</kbd></button>
    <button class="nav-item" data-page="convert" data-label="Convert"><svg class="i"><use href="#i-convert"/></svg>Convert<kbd>2</kbd></button>
    <button class="nav-item" data-page="import" data-label="Import"><svg class="i"><use href="#i-download"/></svg>Import<kbd>3</kbd></button>
    <button class="nav-item" data-page="history" data-label="History"><svg class="i"><use href="#i-history"/></svg>History<kbd>4</kbd></button>
    <button class="nav-item" data-page="settings" data-label="Settings"><svg class="i"><use href="#i-settings"/></svg>Settings<kbd>5</kbd></button>
  </nav>
  <div class="side-foot" role="status">
    <div class="kv"><span>Session</span><span><i class="dot" id="sideDot"></i><span id="sideState">Idle</span></span></div>
    <div class="kv"><span>Checkpoint</span><span id="sideCp">None</span></div>
    <div class="kv"><span>Last run</span><span id="sideLast">Never</span></div>
  </div>
</aside>

<div class="main">
  <header class="topbar">
    <div class="crumbs"><span>Sync console</span><span class="sep">/</span><span id="pageTitle">Export</span></div>
    <div class="top-right">
      <div class="run-meter" id="runMeter" hidden>
        <i class="dot live"></i><span class="pct" id="topPct">0%</span>
        <span class="mini"><i id="topFill"></i></span>
        <span class="eta" id="topEta"></span>
      </div>
      <span class="pill" id="topState"><i class="dot"></i>Idle</span>
    </div>
  </header>

  <main class="content">

    <!-- ═══ EXPORT ═══ -->
    <section class="page active" id="page-export">
      <div class="page-head">
        <div>
          <h1>Export library</h1>
          <p>Pull your MangaDex statuses, ratings and read progress into MyAnimeList, AniList and JSON files.</p>
        </div>
        <div class="head-meta">
          <span class="pill accent" id="headMode">Mode <span class="val">Fast</span></span>
          <span class="pill" id="headFormats">Formats <span class="val">3</span></span>
        </div>
      </div>
      <div class="grid-2">
        <div class="col">
          <div class="card">
            <div class="card-hd">
              <div class="card-t"><span class="step">01 //</span>Credentials</div>
              <div class="card-tools"><span class="sub-note" id="expAuthNote"></span><span class="pill" id="expAuthPill">Unverified</span></div>
            </div>
            <div class="card-bd">
              <div class="field">
                <div class="lbl"><label for="clientId">Client ID</label><span class="h">From mangadex.org/settings</span></div>
                <div class="control"><span class="pre">&gt;</span><input id="clientId" class="mono cred" data-grp="exp" placeholder="personal-client-xxxxxxxx" autocomplete="off" spellcheck="false">
                  <button class="ctl-btn" onclick="paste('clientId')">Paste</button></div>
              </div>
              <div class="field">
                <div class="lbl"><label for="clientSecret">Client secret</label></div>
                <div class="control"><span class="pre">&gt;</span><input id="clientSecret" type="password" class="mono cred" data-grp="exp" placeholder="&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;" autocomplete="off">
                  <button class="ctl-btn icon" onclick="toggleVis('clientSecret', this)" aria-label="Show client secret"><svg class="i"><use href="#i-eye"/></svg></button>
                  <button class="ctl-btn" onclick="paste('clientSecret')">Paste</button></div>
              </div>
              <div class="row-2">
                <div>
                  <div class="lbl"><label for="username">Username</label></div>
                  <div class="control"><input id="username" class="cred" data-grp="exp" placeholder="MangaDex username" autocomplete="username"></div>
                </div>
                <div>
                  <div class="lbl"><label for="password">Password</label></div>
                  <div class="control"><input id="password" type="password" class="cred" data-grp="exp" placeholder="&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;" autocomplete="current-password">
                    <button class="ctl-btn icon" onclick="toggleVis('password', this)" aria-label="Show password"><svg class="i"><use href="#i-eye"/></svg></button></div>
                </div>
              </div>
              <div class="action-row" style="margin-top:14px">
                <button class="btn" id="btnVerifyExp" onclick="verifyCreds('exp')"><svg class="i"><use href="#i-shield"/></svg>Verify credentials</button>
              </div>
            </div>
          </div>

          <div class="card">
            <div class="card-hd">
              <div class="card-t"><span class="step">02 //</span>MyAnimeList profile</div>
              <span class="pill">Optional</span>
            </div>
            <div class="card-bd">
              <div class="row-2">
                <div>
                  <div class="lbl"><label for="malUserId">MAL user ID</label></div>
                  <div class="control"><input id="malUserId" class="mono" placeholder="1428591" inputmode="numeric">
                    <button class="ctl-btn" onclick="paste('malUserId')">Paste</button></div>
                </div>
                <div>
                  <div class="lbl"><label for="malUsername">MAL username</label></div>
                  <div class="control"><input id="malUsername" placeholder="Your MAL username">
                    <button class="ctl-btn" onclick="paste('malUsername')">Paste</button></div>
                </div>
              </div>
              <p class="sub-note" style="margin-top:10px">Only written into the XML header. MAL identifies you by your login when you upload.</p>
            </div>
          </div>

          <div class="card">
            <div class="card-hd">
              <div class="card-t"><span class="step">03 //</span>Export parameters</div>
              <span class="pill" id="paramMode">Profile <span class="val">Fast</span></span>
            </div>
            <div class="card-bd">
              <div class="sect-lbl"><span>Extraction mode</span></div>
              <div class="seg" role="radiogroup" aria-label="Extraction mode">
                <button class="seg-opt active" data-mode="fast" role="radio" aria-checked="true" onclick="setMode('fast')">
                  <span class="t"><svg class="i"><use href="#i-bolt"/></svg>Fast</span>
                  <span class="s">Statuses, ratings and titles. Done in minutes.</span>
                </button>
                <button class="seg-opt" data-mode="deep" role="radio" aria-checked="false" onclick="setMode('deep')">
                  <span class="t"><svg class="i"><use href="#i-layers"/></svg>Deep</span>
                  <span class="s">Adds last read chapter and volume. Much slower.</span>
                </button>
              </div>
              <div class="field" style="margin-top:14px">
                <div class="lbl"><label for="saveDir">Save folder</label><span class="h">Blank saves to the working directory</span></div>
                <div class="control"><span class="pre"><svg class="i"><use href="#i-folder"/></svg></span><input id="saveDir" class="mono" placeholder="/path/to/exports">
                  <button class="ctl-btn" onclick="browseFolder('saveDir')">Browse</button></div>
              </div>
              <div class="sect-lbl" style="margin-top:14px"><span>Output formats</span><span class="h">XLSX is always written for Convert</span></div>
              <div class="opts">
                <label class="opt"><input type="checkbox" id="fmtMal" checked onchange="refreshHeadMeta()"><span><span class="ot">MAL XML</span><span class="os">Plus a compressed .gz copy</span></span></label>
                <label class="opt"><input type="checkbox" id="fmtAl" checked onchange="refreshHeadMeta()"><span><span class="ot">AniList XML</span><span class="os">Same schema, for AniList's importer</span></span></label>
                <label class="opt"><input type="checkbox" id="fmtJson" checked onchange="refreshHeadMeta()"><span><span class="ot">JSON backup</span><span class="os">Full data, used by Import</span></span></label>
                <label class="opt"><input type="checkbox" id="dryRun"><span><span class="ot">Dry run</span><span class="os">Simulate the run, write no files</span></span></label>
              </div>
            </div>
          </div>
        </div>

        <div class="col">
          <div class="card">
            <div class="card-hd">
              <div class="card-t"><span class="step">04 //</span>Extract</div>
              <div class="card-tools">
                <span class="pill" id="libTotal" hidden>Library <span class="val" id="libTotalN">0</span></span>
                <button class="btn btn-sm" id="btnCounts" onclick="refreshCounts()"><svg class="i"><use href="#i-refresh"/></svg>Load counts</button>
              </div>
            </div>
            <div class="card-bd">
              <div class="sect-lbl"><span>Export one status</span><span class="h" id="countsHint">Counts load from your library</span></div>
              <div class="st-grid" id="statusChips">
                <button class="st" data-status="reading" onclick="startExport('reading')">Reading<span class="cnt" id="count-reading"><svg class="i go"><use href="#i-arrow"/></svg></span></button>
                <button class="st" data-status="completed" onclick="startExport('completed')">Completed<span class="cnt" id="count-completed"><svg class="i go"><use href="#i-arrow"/></svg></span></button>
                <button class="st" data-status="on_hold" onclick="startExport('on_hold')">On-hold<span class="cnt" id="count-on_hold"><svg class="i go"><use href="#i-arrow"/></svg></span></button>
                <button class="st" data-status="dropped" onclick="startExport('dropped')">Dropped<span class="cnt" id="count-dropped"><svg class="i go"><use href="#i-arrow"/></svg></span></button>
                <button class="st" data-status="plan_to_read" onclick="startExport('plan_to_read')">Plan to read<span class="cnt" id="count-plan_to_read"><svg class="i go"><use href="#i-arrow"/></svg></span></button>
                <button class="st" data-status="re_reading" onclick="startExport('re_reading')">Re-reading<span class="cnt" id="count-re_reading"><svg class="i go"><use href="#i-arrow"/></svg></span></button>
              </div>
              <div class="divider"></div>
              <div class="action-row">
                <button class="btn btn-primary btn-lg grow" id="btnAll" onclick="startExport(null)"><svg class="i"><use href="#i-bolt"/></svg>Extract entire library<span class="count" id="btnAllCount"></span></button>
                <button class="btn btn-lg" id="btnResume" onclick="resumeExport()" disabled><svg class="i"><use href="#i-play"/></svg>Resume</button>
                <button class="btn btn-lg btn-danger" id="btnStop" onclick="stopRun()" disabled><svg class="i"><use href="#i-stop"/></svg>Stop</button>
              </div>
              <div class="hints"><span><kbd>Ctrl</kbd><kbd>Enter</kbd> Extract all</span><span><kbd>Esc</kbd> Stop</span></div>
            </div>
          </div>

          <div class="card">
            <div class="card-hd">
              <div class="card-t"><svg class="i"><use href="#i-chart"/></svg>Progress</div>
              <span class="pill" data-state-pill><i class="dot"></i>Idle</span>
            </div>
            <div class="card-bd">
              <div class="prog-top">
                <div class="prog-pct idle" id="progPct">0<small>%</small></div>
                <div class="prog-label" id="progLabel">Nothing running. Start an export to see live progress.</div>
              </div>
              <div class="track"><div class="fill" id="progFill" role="progressbar" aria-label="Export progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"></div></div>
              <div class="prog-meta">
                <div><span class="k">ETA</span><span class="v" id="progEta">-</span></div>
                <div><span class="k">Mode</span><span class="v" id="progMode">Fast</span></div>
                <div><span class="k">Checkpoint</span><span class="v" data-cp-text>None</span></div>
              </div>
            </div>
          </div>

          <div class="card">
            <div class="card-hd">
              <div class="card-t"><svg class="i"><use href="#i-log"/></svg>Live log</div>
              <div class="card-tools">
                <span class="pill" id="logBoxCount">0 lines</span>
                <button class="pill toggle-pill ok" id="logBoxAuto" onclick="toggleAuto('logBox')" aria-pressed="true">Autoscroll on</button>
                <button class="btn btn-sm btn-ghost" onclick="clearLog('logBox')"><svg class="i"><use href="#i-x"/></svg>Clear</button>
              </div>
            </div>
            <div class="log-wrap">
              <div class="log-box" id="logBox" role="log" aria-live="polite" aria-label="Export log"></div>
              <div class="log-empty" id="logBoxEmpty"><svg class="i"><use href="#i-log"/></svg><span class="t">Waiting for activity<span class="caret"></span></span><span class="s">Auth, fetch and file events stream here the moment a run starts.</span></div>
            </div>
          </div>

          <div class="card" id="skippedCard" hidden>
            <div class="card-hd">
              <div class="card-t"><svg class="i"><use href="#i-warning"/></svg>Skipped: no MAL ID</div>
              <div class="card-tools"><span class="pill warn" id="skippedCount">0 titles</span>
                <button class="btn btn-sm btn-ghost" onclick="copyList(lastSkipped)"><svg class="i"><use href="#i-copy"/></svg>Copy all</button></div>
            </div>
            <div class="card-bd flush"><div class="row-list" id="skippedList"></div></div>
          </div>
        </div>
      </div>
    </section>

    <!-- ═══ CONVERT ═══ -->
    <section class="page" id="page-convert">
      <div class="page-head">
        <div>
          <h1>Convert exports</h1>
          <p>Turn exported .xlsx files into MyAnimeList and AniList import files, without touching the API.</p>
        </div>
        <div class="head-meta"><span class="pill" id="convFilesPill">Files <span class="val">0</span></span></div>
      </div>
      <div class="grid-2">
        <div class="col">
          <div class="card">
            <div class="card-hd">
              <div class="card-t"><span class="step">01 //</span>MyAnimeList profile</div>
              <span class="pill accent">Required</span>
            </div>
            <div class="card-bd">
              <div class="row-2">
                <div>
                  <div class="lbl"><label for="convMalId">MAL user ID</label></div>
                  <div class="control"><input id="convMalId" class="mono" placeholder="1428591" inputmode="numeric">
                    <button class="ctl-btn" onclick="paste('convMalId')">Paste</button></div>
                </div>
                <div>
                  <div class="lbl"><label for="convMalName">MAL username</label></div>
                  <div class="control"><input id="convMalName" placeholder="Your MAL username">
                    <button class="ctl-btn" onclick="paste('convMalName')">Paste</button></div>
                </div>
              </div>
              <div class="action-row" style="margin-top:12px"><button class="btn btn-sm btn-ghost" onclick="copyMalFromExport()"><svg class="i"><use href="#i-copy"/></svg>Use values from Export</button></div>
            </div>
          </div>
          <div class="card">
            <div class="card-hd"><div class="card-t"><span class="step">02 //</span>Output</div></div>
            <div class="card-bd">
              <div class="field">
                <div class="lbl"><label for="convSaveDir">Output folder</label><span class="h">Auto-fill uses the export folder</span></div>
                <div class="control"><span class="pre"><svg class="i"><use href="#i-folder"/></svg></span><input id="convSaveDir" class="mono" placeholder="/path/to/output">
                  <button class="ctl-btn" onclick="browseFolder('convSaveDir')">Browse</button></div>
              </div>
              <div class="opts" style="margin-top:14px">
                <label class="opt"><input type="checkbox" id="convMal" checked><span><span class="ot">MAL XML</span><span class="os">Plus a compressed .gz copy</span></span></label>
                <label class="opt"><input type="checkbox" id="convAl" checked><span><span class="ot">AniList XML</span><span class="os">For AniList's importer</span></span></label>
                <label class="opt"><input type="checkbox" id="convScores" checked><span><span class="ot">Include scores</span><span class="os">Carry your 1-10 ratings</span></span></label>
                <label class="opt"><input type="checkbox" id="convDry"><span><span class="ot">Dry run</span><span class="os">Count entries, write nothing</span></span></label>
              </div>
            </div>
          </div>
          <div class="card">
            <div class="card-hd"><div class="card-t"><span class="step">03 //</span>Generate</div><span class="pill" id="convState">Ready</span></div>
            <div class="card-bd">
              <button class="btn btn-ok btn-lg" style="width:100%" id="btnGenerate" onclick="generateXml()"><svg class="i"><use href="#i-bolt"/></svg>Generate import files</button>
              <div id="convResult" style="margin-top:12px" hidden></div>
            </div>
          </div>
        </div>
        <div class="col">
          <div class="card">
            <div class="card-hd">
              <div class="card-t"><svg class="i"><use href="#i-sheet"/></svg>Excel files</div>
              <div class="card-tools">
                <button class="btn btn-sm" onclick="autoFill()"><svg class="i"><use href="#i-refresh"/></svg>Auto-fill</button>
                <button class="btn btn-sm" onclick="addFile()"><svg class="i"><use href="#i-plus"/></svg>Add</button>
                <button class="btn btn-sm btn-ghost" onclick="clearFiles()" aria-label="Clear all files"><svg class="i"><use href="#i-x"/></svg></button>
              </div>
            </div>
            <div class="card-bd flush"><div class="row-list" id="fileList" style="max-height:360px"></div></div>
          </div>
          <div class="card">
            <div class="card-hd">
              <div class="card-t"><svg class="i"><use href="#i-warning"/></svg>Skipped manga</div>
              <span class="pill" id="convSkipPill">None yet</span>
            </div>
            <div class="card-bd flush"><div class="row-list" id="skippedBox"></div></div>
          </div>
        </div>
      </div>
    </section>

    <!-- ═══ IMPORT ═══ -->
    <section class="page" id="page-import">
      <div class="page-head">
        <div>
          <h1>Import library</h1>
          <p>Write statuses and ratings from a MAL or AniList XML, or a JSON backup, into your MangaDex account.</p>
        </div>
        <div class="head-meta"><span class="pill info" id="impTypePill">Source <span class="val">XML</span></span></div>
      </div>
      <div class="grid-2">
        <div class="col">
          <div class="card">
            <div class="card-hd">
              <div class="card-t"><span class="step">01 //</span>Credentials</div>
              <div class="card-tools"><span class="sub-note" id="impAuthNote"></span><span class="pill" id="impAuthPill">Unverified</span></div>
            </div>
            <div class="card-bd">
              <div class="field">
                <div class="lbl"><label for="impClientId">Client ID</label></div>
                <div class="control"><span class="pre">&gt;</span><input id="impClientId" class="mono cred" data-grp="imp" placeholder="personal-client-xxxxxxxx" autocomplete="off" spellcheck="false">
                  <button class="ctl-btn" onclick="paste('impClientId')">Paste</button></div>
              </div>
              <div class="field">
                <div class="lbl"><label for="impClientSecret">Client secret</label></div>
                <div class="control"><span class="pre">&gt;</span><input id="impClientSecret" type="password" class="mono cred" data-grp="imp" placeholder="&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;" autocomplete="off">
                  <button class="ctl-btn icon" onclick="toggleVis('impClientSecret', this)" aria-label="Show client secret"><svg class="i"><use href="#i-eye"/></svg></button>
                  <button class="ctl-btn" onclick="paste('impClientSecret')">Paste</button></div>
              </div>
              <div class="row-2">
                <div>
                  <div class="lbl"><label for="impUsername">Username</label></div>
                  <div class="control"><input id="impUsername" class="cred" data-grp="imp" placeholder="MangaDex username" autocomplete="username"></div>
                </div>
                <div>
                  <div class="lbl"><label for="impPassword">Password</label></div>
                  <div class="control"><input id="impPassword" type="password" class="cred" data-grp="imp" placeholder="&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;" autocomplete="current-password">
                    <button class="ctl-btn icon" onclick="toggleVis('impPassword', this)" aria-label="Show password"><svg class="i"><use href="#i-eye"/></svg></button></div>
                </div>
              </div>
              <div class="action-row" style="margin-top:14px">
                <button class="btn" id="btnVerifyImp" onclick="verifyCreds('imp')"><svg class="i"><use href="#i-shield"/></svg>Verify credentials</button>
                <button class="btn btn-ghost" onclick="copyCredsFromExport()"><svg class="i"><use href="#i-copy"/></svg>Use Export credentials</button>
              </div>
            </div>
          </div>

          <div class="card">
            <div class="card-hd"><div class="card-t"><span class="step">02 //</span>Source file</div></div>
            <div class="card-bd">
              <div class="seg" role="radiogroup" aria-label="Source file type">
                <button class="seg-opt active" data-imptype="xml" role="radio" aria-checked="true" onclick="setImpType('xml')">
                  <span class="t"><svg class="i"><use href="#i-code"/></svg>XML</span>
                  <span class="s">mal_*.xml or anilist_*.xml exports</span>
                </button>
                <button class="seg-opt" data-imptype="json" role="radio" aria-checked="false" onclick="setImpType('json')">
                  <span class="t"><svg class="i"><use href="#i-braces"/></svg>JSON backup</span>
                  <span class="s">mdex_*.json from the Export tab</span>
                </button>
              </div>
              <div class="field" style="margin-top:14px">
                <div class="lbl"><label for="impFilePath">File path</label></div>
                <div class="control"><span class="pre"><svg class="i"><use href="#i-file"/></svg></span><input id="impFilePath" class="mono" placeholder="/path/to/mal_reading.xml">
                  <button class="ctl-btn" onclick="browseFile('impFilePath')">Browse</button>
                  <button class="ctl-btn" onclick="paste('impFilePath')">Paste</button></div>
              </div>
              <div class="callout warn" id="impXmlNote" style="margin-top:12px">
                <svg class="i"><use href="#i-warning"/></svg>
                <span><span class="ct">Slow path</span><span class="cs">Every title needs a MangaDex lookup by MAL ID. Large libraries (500+) take 10 to 30 minutes.</span></span>
              </div>
              <div class="callout ok" id="impJsonNote" style="margin-top:12px" hidden>
                <svg class="i"><use href="#i-check-circle"/></svg>
                <span><span class="ct">Fast path</span><span class="cs">Uses the MangaDex IDs stored in the backup, so no lookups are needed.</span></span>
              </div>
            </div>
          </div>

          <div class="card">
            <div class="card-hd"><div class="card-t"><span class="step">03 //</span>Options</div></div>
            <div class="card-bd">
              <div class="opts">
                <label class="opt"><input type="checkbox" id="impScores" checked><span><span class="ot">Import ratings</span><span class="os">Writes 1-10 scores to MangaDex</span></span></label>
                <label class="opt"><input type="checkbox" id="impDry"><span><span class="ot">Dry run</span><span class="os">Resolve titles, change nothing</span></span></label>
              </div>
            </div>
          </div>
        </div>

        <div class="col">
          <div class="card">
            <div class="card-hd"><div class="card-t"><span class="step">04 //</span>Run import</div><span class="pill" data-state-pill><i class="dot"></i>Idle</span></div>
            <div class="card-bd">
              <div class="callout info" style="margin-bottom:14px">
                <svg class="i"><use href="#i-info"/></svg>
                <span><span class="ct">Writes to your account</span><span class="cs">Sets statuses and ratings on MangaDex. Existing entries are updated. Titles that can't be found are skipped and listed below.</span></span>
              </div>
              <div class="action-row">
                <button class="btn btn-primary btn-lg grow" id="btnImport" onclick="startImport()"><svg class="i"><use href="#i-download"/></svg>Start import</button>
                <button class="btn btn-lg btn-danger" id="btnImportStop" onclick="stopRun()" disabled><svg class="i"><use href="#i-stop"/></svg>Stop</button>
              </div>
              <div class="hints"><span><kbd>Ctrl</kbd><kbd>Enter</kbd> Start import</span><span><kbd>Esc</kbd> Stop</span></div>
            </div>
          </div>

          <div class="card">
            <div class="card-hd"><div class="card-t"><svg class="i"><use href="#i-chart"/></svg>Progress</div><span class="pill" data-state-pill><i class="dot"></i>Idle</span></div>
            <div class="card-bd">
              <div class="prog-top">
                <div class="prog-pct idle" id="impProgPct">0<small>%</small></div>
                <div class="prog-label" id="impProgLabel">Nothing running. Start an import to see live progress.</div>
              </div>
              <div class="track"><div class="fill" id="impProgFill" role="progressbar" aria-label="Import progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"></div></div>
            </div>
          </div>

          <div class="card">
            <div class="card-hd">
              <div class="card-t"><svg class="i"><use href="#i-log"/></svg>Live log</div>
              <div class="card-tools">
                <span class="pill" id="logBox2Count">0 lines</span>
                <button class="pill toggle-pill ok" id="logBox2Auto" onclick="toggleAuto('logBox2')" aria-pressed="true">Autoscroll on</button>
                <button class="btn btn-sm btn-ghost" onclick="clearLog('logBox2')"><svg class="i"><use href="#i-x"/></svg>Clear</button>
              </div>
            </div>
            <div class="log-wrap">
              <div class="log-box" id="logBox2" role="log" aria-live="polite" aria-label="Import log"></div>
              <div class="log-empty" id="logBox2Empty"><svg class="i"><use href="#i-log"/></svg><span class="t">Waiting for activity<span class="caret"></span></span><span class="s">Lookups, status writes and skips stream here during an import.</span></div>
            </div>
          </div>

          <div class="card" id="impSkippedCard" hidden>
            <div class="card-hd">
              <div class="card-t"><svg class="i"><use href="#i-warning"/></svg>Skipped</div>
              <div class="card-tools"><span class="pill warn" id="impSkippedCount">0 titles</span>
                <button class="btn btn-sm btn-ghost" onclick="copyList(lastSkipped)"><svg class="i"><use href="#i-copy"/></svg>Copy all</button></div>
            </div>
            <div class="card-bd flush"><div class="row-list" id="impSkippedList"></div></div>
          </div>
        </div>
      </div>
    </section>

    <!-- ═══ HISTORY ═══ -->
    <section class="page" id="page-history">
      <div class="page-head">
        <div>
          <h1>Run history</h1>
          <p>Every export, import and convert run on this machine, newest first.</p>
        </div>
        <div class="head-meta">
          <button class="btn btn-sm" onclick="loadHistory()"><svg class="i"><use href="#i-refresh"/></svg>Refresh</button>
          <button class="btn btn-sm btn-danger" onclick="clearHistory()"><svg class="i"><use href="#i-trash"/></svg>Clear history</button>
        </div>
      </div>
      <div class="stats">
        <div class="stat"><div class="stat-hd">Total runs<svg class="i"><use href="#i-history"/></svg></div><div class="stat-v" id="statRuns">0</div><div class="stat-s" id="statRunsSub">No runs yet</div></div>
        <div class="stat"><div class="stat-hd">Titles processed<svg class="i"><use href="#i-layers"/></svg></div><div class="stat-v" id="statTitles">0</div><div class="stat-s" id="statTitlesSub">Across all runs</div></div>
        <div class="stat"><div class="stat-hd">Avg duration<svg class="i"><use href="#i-clock"/></svg></div><div class="stat-v" id="statElapsed">-</div><div class="stat-s" id="statElapsedSub">Export and import runs</div></div>
        <div class="stat ok"><div class="stat-hd">Match rate<svg class="i"><use href="#i-check-circle"/></svg></div><div class="stat-v" id="statRate">-</div><div class="stat-s" id="statRateSub">Titles not skipped</div></div>
      </div>
      <div class="card">
        <div class="filterbar">
          <div class="control"><span class="pre">&gt;</span><input id="historySearch" class="mono" placeholder="Filter by date, type, mode or file name" oninput="renderHistory()"></div>
          <div class="tabs" role="tablist" id="histTabs">
            <button class="tab active" data-f="all" onclick="setHistFilter('all')">All<span class="n" id="hn-all">0</span></button>
            <button class="tab" data-f="export" onclick="setHistFilter('export')">Export<span class="n" id="hn-export">0</span></button>
            <button class="tab" data-f="import" onclick="setHistFilter('import')">Import<span class="n" id="hn-import">0</span></button>
            <button class="tab" data-f="convert" onclick="setHistFilter('convert')">Convert<span class="n" id="hn-convert">0</span></button>
          </div>
        </div>
        <div class="tbl-wrap">
          <table>
            <thead><tr><th>Date</th><th>Operation</th><th>Scope</th><th class="r">Total</th><th class="r">Skipped</th><th>Mode</th><th class="r">Elapsed</th><th>Files</th></tr></thead>
            <tbody id="historyBody"></tbody>
          </table>
        </div>
        <div class="empty" id="historyEmpty" hidden><svg class="i"><use href="#i-history"/></svg><span class="t" id="historyEmptyT">No runs yet</span><span class="s" id="historyEmptyS">Your first export, import or convert shows up here with totals, timing and output files.</span></div>
        <div class="tbl-foot"><span id="histFoot">Showing 0 of 0 runs</span><span>Stored in mdex_history.json (last 100)</span></div>
      </div>
    </section>

    <!-- ═══ SETTINGS ═══ -->
    <section class="page" id="page-settings">
      <div class="page-head">
        <div>
          <h1>Settings</h1>
          <p>Defaults, the saved resume checkpoint, and details about this local install.</p>
        </div>
      </div>
      <div class="grid-2">
        <div class="col">
          <div class="card">
            <div class="card-hd"><div class="card-t"><svg class="i"><use href="#i-layers"/></svg>Default mode</div><span class="pill accent" id="setModePill">Fast</span></div>
            <div class="card-bd">
              <div class="seg" role="radiogroup" aria-label="Default export mode">
                <button class="seg-opt active" data-smode="fast" role="radio" aria-checked="true" onclick="setSettingsMode('fast')">
                  <span class="t"><svg class="i"><use href="#i-bolt"/></svg>Fast</span><span class="s">Recommended for most libraries</span>
                </button>
                <button class="seg-opt" data-smode="deep" role="radio" aria-checked="false" onclick="setSettingsMode('deep')">
                  <span class="t"><svg class="i"><use href="#i-layers"/></svg>Deep</span><span class="s">Includes last read chapter and volume</span>
                </button>
              </div>
            </div>
          </div>
          <div class="card">
            <div class="card-hd"><div class="card-t"><svg class="i"><use href="#i-bookmark"/></svg>Resume checkpoint</div><span class="pill" id="cpPill">None</span></div>
            <div class="card-bd">
              <p style="color:var(--text-2)" id="cpInfo">No checkpoint on disk.</p>
              <p class="sub-note" style="margin-top:6px">Saved after each finished status group, so an interrupted export can resume from the Export tab.</p>
              <div class="action-row" style="margin-top:14px"><button class="btn btn-danger" id="btnClearCp" onclick="clearCheckpoint()"><svg class="i"><use href="#i-trash"/></svg>Clear checkpoint</button></div>
            </div>
          </div>
          <div class="card">
            <div class="card-hd"><div class="card-t"><svg class="i"><use href="#i-keyboard"/></svg>Keyboard shortcuts</div></div>
            <div class="card-bd flush kv-list">
              <div class="kv-row"><span class="k">Switch page</span><span class="v"><kbd>1</kbd> <kbd>2</kbd> <kbd>3</kbd> <kbd>4</kbd> <kbd>5</kbd></span></div>
              <div class="kv-row"><span class="k">Start run</span><span class="v"><kbd>Ctrl</kbd> <kbd>Enter</kbd> on Export or Import</span></div>
              <div class="kv-row"><span class="k">Stop run</span><span class="v"><kbd>Esc</kbd></span></div>
            </div>
          </div>
        </div>
        <div class="col">
          <div class="card">
            <div class="card-hd"><div class="card-t"><svg class="i"><use href="#i-info"/></svg>About this install</div><span class="pill ok"><i class="dot ok"></i>Local</span></div>
            <div class="card-bd flush kv-list">
              <div class="kv-row"><span class="k">Version</span><span class="v" id="infoVer">-</span></div>
              <div class="kv-row"><span class="k">Local URL</span><span class="v" id="infoUrl">-</span></div>
              <div class="kv-row"><span class="k">Working dir</span><span class="v wrap" id="infoCwd">-</span></div>
              <div class="kv-row"><span class="k">History file</span><span class="v wrap" id="infoHist">-</span></div>
              <div class="kv-row"><span class="k">Checkpoint file</span><span class="v wrap" id="infoCp">-</span></div>
              <div class="kv-row"><span class="k">Platform</span><span class="v" id="infoPlat">-</span></div>
            </div>
          </div>
          <div class="card">
            <div class="card-hd"><div class="card-t"><svg class="i"><use href="#i-bolt"/></svg>Modes explained</div></div>
            <div class="card-bd">
              <div class="opts one">
                <div class="opt" style="cursor:default"><span><span class="ot">Fast</span><span class="os">Fetches statuses, titles, links and ratings in batches of 100. A few minutes even for large libraries.</span></span></div>
                <div class="opt" style="cursor:default"><span><span class="ot">Deep</span><span class="os">Also resolves every chapter you've read to find the last chapter and volume, including chapters later removed by takedowns.</span></span></div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>

  </main>
</div>

<div id="toast" role="status" aria-live="polite"></div>

<script>
// ── State ────────────────────────────────────────────────────────────────────
let mode = 'fast';
let impType = 'xml';
let convFiles = [];      // [{path, status}]
let historyRows = [];
let histFilter = 'all';
let lastSkipped = [];
let wasRunning = false;
let lastRunEndedAt = null;
let libCounts = null;
const autoScroll = { logBox: true, logBox2: true };
const lineCount  = { logBox: 0, logBox2: 0 };
const $ = id => document.getElementById(id);

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
async function postJSON(url, body) {
  const r = await fetch(url, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {}) });
  let d = {};
  try { d = await r.json(); } catch (e) {}
  return d;
}
const icon = (id, cls) => `<svg class="i ${cls||''}" aria-hidden="true"><use href="#${id}"/></svg>`;

// ── Navigation ───────────────────────────────────────────────────────────────
function goPage(pg) {
  const el = document.querySelector(`.nav-item[data-page="${pg}"]`);
  if (!el) return;
  document.querySelectorAll('.nav-item').forEach(n => { n.classList.toggle('active', n === el); n.removeAttribute('aria-current'); });
  el.setAttribute('aria-current', 'page');
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('active', p.id === 'page-' + pg));
  $('pageTitle').textContent = el.dataset.label;
  if (location.hash !== '#' + pg) history.replaceState(null, '', '#' + pg);
  if (pg === 'history') loadHistory();
  if (pg === 'settings') { loadCpInfo(); loadInfo(); }
}
document.querySelectorAll('.nav-item').forEach(el => el.addEventListener('click', () => goPage(el.dataset.page)));

// ── Log stream ───────────────────────────────────────────────────────────────
const TAG = { info: 'INFO', success: 'OK', warning: 'WARN', error: 'ERR' };
function appendLog(boxId, d) {
  const box = $(boxId);
  const tag = d.tag || 'info';
  const line = document.createElement('div');
  const sect = /^\u2500\u2500\s*(.+?)\s*\u2500\u2500$/.exec(d.msg || '');
  if (sect) {
    line.className = 'log-sect';
    line.innerHTML = `<span>${escHtml(sect[1])}</span>`;
  } else {
    // The worker prefixes messages with its own status glyph; the [TAG] already says that.
    const msg = String(d.msg || '').replace(/^[\u2713\u2714\u26a0\u2717\u2718]\s*/, '');
    line.className = 'log-line ' + tag;
    line.innerHTML = `<span class="ts">${escHtml(d.ts)}</span><span class="tg">[${TAG[tag] || tag.toUpperCase()}]</span><span class="m">${escHtml(msg)}</span>`;
  }
  box.appendChild(line);
  lineCount[boxId]++;
  $(boxId + 'Count').textContent = lineCount[boxId] + (lineCount[boxId] === 1 ? ' line' : ' lines');
  $(boxId + 'Empty').hidden = true;
  if (autoScroll[boxId]) box.scrollTop = box.scrollHeight;
}
function clearLog(boxId) {
  $(boxId).innerHTML = '';
  lineCount[boxId] = 0;
  $(boxId + 'Count').textContent = '0 lines';
  $(boxId + 'Empty').hidden = false;
}
function toggleAuto(boxId) {
  autoScroll[boxId] = !autoScroll[boxId];
  const b = $(boxId + 'Auto');
  b.textContent = autoScroll[boxId] ? 'Autoscroll on' : 'Autoscroll off';
  b.classList.toggle('ok', autoScroll[boxId]);
  b.setAttribute('aria-pressed', autoScroll[boxId]);
  if (autoScroll[boxId]) $(boxId).scrollTop = $(boxId).scrollHeight;
}
const evtSrc = new EventSource('/api/stream');
evtSrc.onmessage = e => {
  const d = JSON.parse(e.data);
  if (d.ping) return;
  appendLog('logBox', d);
  appendLog('logBox2', d);
};

// ── Status polling ───────────────────────────────────────────────────────────
function setStatePills(kind, text) {
  const cls = kind === 'run' ? 'pill accent' : kind === 'done' ? 'pill ok' : 'pill';
  const dot = kind === 'run' ? 'dot live' : kind === 'done' ? 'dot ok' : 'dot';
  const html = `<i class="${dot}"></i>${text}`;
  document.querySelectorAll('[data-state-pill]').forEach(p => { p.className = cls; p.innerHTML = html; });
  $('topState').className = cls; $('topState').innerHTML = html;
  $('sideDot').className = dot; $('sideState').textContent = text;
}
function setProgress(prefix, pct, label, idleText) {
  const p = Math.max(0, Math.min(100, pct || 0));
  const fill = $(prefix + 'ProgFill') || $('progFill');
  const pctEl = $(prefix + 'ProgPct') || $('progPct');
  const lblEl = $(prefix + 'ProgLabel') || $('progLabel');
  fill.style.transform = `scaleX(${p / 100})`;
  fill.setAttribute('aria-valuenow', Math.round(p));
  pctEl.innerHTML = `${p >= 10 ? Math.round(p) : p.toFixed(1).replace(/\.0$/, '')}<small>%</small>`;
  pctEl.classList.toggle('idle', !label);
  lblEl.textContent = label || idleText;
}
function fmtTime(d) { return d.toTimeString().slice(0, 5); }

async function pollStatus() {
  let d;
  try { d = await (await fetch('/api/status')).json(); } catch (e) { return; }
  const running = !!d.running;

  if (wasRunning && !running) { lastRunEndedAt = new Date(); loadHistory(); }
  wasRunning = running;

  if (running) {
    setStatePills('run', 'Running');
    $('runMeter').hidden = false;
    $('topPct').textContent = Math.round(d.progress || 0) + '%';
    $('topFill').style.transform = `scaleX(${(d.progress || 0) / 100})`;
    $('topEta').textContent = d.eta ? 'ETA ' + d.eta : '';
  } else {
    $('runMeter').hidden = true;
    if (lastRunEndedAt) setStatePills('done', 'Finished ' + fmtTime(lastRunEndedAt));
    else setStatePills('idle', 'Idle');
  }

  const idleExp = lastRunEndedAt ? `Last run finished at ${fmtTime(lastRunEndedAt)}. Details are in the log.` : 'Nothing running. Start an export to see live progress.';
  const idleImp = lastRunEndedAt ? `Last run finished at ${fmtTime(lastRunEndedAt)}. Details are in the log.` : 'Nothing running. Start an import to see live progress.';
  setProgress('', running ? d.progress : 0, running ? d.label : '', idleExp);
  setProgress('imp', running ? d.progress : 0, running ? d.label : '', idleImp);
  $('progEta').textContent = running && d.eta ? d.eta : '-';

  $('btnAll').disabled = running;
  $('btnStop').disabled = !running;
  $('btnResume').disabled = running || !d.has_checkpoint;
  $('btnImport').disabled = running;
  $('btnImportStop').disabled = !running;
  $('btnCounts').disabled = running;
  document.querySelectorAll('.st').forEach(c => c.disabled = running);

  const cpText = d.has_checkpoint ? ((d.checkpoint_done || []).length ? 'Saved: ' + d.checkpoint_done.join(', ') : 'Saved') : 'None';
  $('sideCp').textContent = d.has_checkpoint ? 'Saved' : 'None';
  document.querySelectorAll('[data-cp-text]').forEach(el => el.textContent = cpText);

  const skipped = d.skipped || [];
  if (skipped.join('\u0001') !== lastSkipped.join('\u0001')) {
    lastSkipped = skipped.slice();
    renderSkipRows('skippedList', skipped);
    renderSkipRows('impSkippedList', skipped);
    const label = skipped.length + (skipped.length === 1 ? ' title' : ' titles');
    $('skippedCount').textContent = label;
    $('impSkippedCount').textContent = label;
  }
  $('skippedCard').hidden = !skipped.length;
  $('impSkippedCard').hidden = !skipped.length;
}
setInterval(pollStatus, 800);

function renderSkipRows(listId, titles) {
  $(listId).innerHTML = titles.map((t, i) => `
    <div class="row">
      <span class="idx">${String(i + 1).padStart(2, '0')}</span>
      <span class="ttl" title="${escHtml(t)}">${escHtml(t)}</span>
      <span class="acts">
        <button class="btn btn-sm btn-ghost" data-t="${escHtml(t)}" onclick="searchMal(this.dataset.t)">${icon('i-external')}Search MAL</button>
        <button class="btn btn-sm btn-ghost" data-t="${escHtml(t)}" onclick="copyText(this.dataset.t)" aria-label="Copy title">${icon('i-copy')}</button>
      </span>
    </div>`).join('');
}
async function searchMal(title) {
  const url = 'https://myanimelist.net/manga.php?cat=manga&q=' + encodeURIComponent(title);
  const d = await postJSON('/api/open_url', { url });
  if (!d.ok) toast('Could not open the browser.', 'err');
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch (e) {
    const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta);
    ta.select(); try { document.execCommand('copy'); } catch (e2) {} ta.remove();
  }
  toast('Copied to clipboard.', 'ok');
}
function copyList(list) { if (list.length) copyText(list.join('\n')); }

// ── Mode / head meta ─────────────────────────────────────────────────────────
function setMode(m) {
  mode = m;
  document.querySelectorAll('.seg-opt[data-mode]').forEach(b => { const on = b.dataset.mode === m; b.classList.toggle('active', on); b.setAttribute('aria-checked', on); });
  document.querySelectorAll('.seg-opt[data-smode]').forEach(b => { const on = b.dataset.smode === m; b.classList.toggle('active', on); b.setAttribute('aria-checked', on); });
  const label = m === 'deep' ? 'Deep' : 'Fast';
  $('headMode').innerHTML = `Mode <span class="val">${label}</span>`;
  $('paramMode').innerHTML = `Profile <span class="val">${label}</span>`;
  $('progMode').textContent = label;
  $('setModePill').textContent = label;
}
function setSettingsMode(m) { setMode(m); }
function refreshHeadMeta() {
  const n = ['fmtMal', 'fmtAl', 'fmtJson'].filter(id => $(id).checked).length + 1;  // +1: XLSX always written
  $('headFormats').innerHTML = `Formats <span class="val">${n}</span>`;
}

// ── Credentials ──────────────────────────────────────────────────────────────
function creds() {
  return {
    client_id: $('clientId').value.trim(), client_secret: $('clientSecret').value.trim(),
    username: $('username').value.trim(), password: $('password').value.trim(),
    mal_user_id: $('malUserId').value.trim(), mal_username: $('malUsername').value.trim(),
    save_dir: $('saveDir').value.trim(), mode: mode,
    fmt_mal: $('fmtMal').checked, fmt_al: $('fmtAl').checked, fmt_json: $('fmtJson').checked,
    dry_run: $('dryRun').checked,
  };
}
function impCreds() {
  return { client_id: $('impClientId').value.trim(), client_secret: $('impClientSecret').value.trim(),
           username: $('impUsername').value.trim(), password: $('impPassword').value.trim() };
}
function haveAll(c) { return c.client_id && c.client_secret && c.username && c.password; }
function setAuth(grp, state, note) {
  const pill = $(grp + 'AuthPill'), n = $(grp + 'AuthNote');
  pill.className = 'pill' + (state === 'ok' ? ' ok' : state === 'err' ? ' err' : state === 'busy' ? ' accent' : '');
  pill.innerHTML = state === 'ok' ? `<i class="dot ok"></i>Verified` : state === 'err' ? 'Rejected' : state === 'busy' ? '<i class="dot live"></i>Checking' : 'Unverified';
  n.textContent = note || '';
}
async function verifyCreds(grp) {
  const c = grp === 'exp' ? creds() : impCreds();
  if (!haveAll(c)) { toast('Fill in all four credential fields first.', 'err'); return; }
  const btn = $(grp === 'exp' ? 'btnVerifyExp' : 'btnVerifyImp');
  btn.disabled = true; setAuth(grp, 'busy');
  const d = await postJSON('/api/test_credentials', c);
  btn.disabled = false;
  if (d.ok) {
    const mins = Math.max(1, Math.round((d.expires_in || 0) / 60));
    setAuth(grp, 'ok', `token ${mins}m`);
    toast('Credentials verified with MangaDex.', 'ok');
  } else {
    setAuth(grp, 'err');
    toast(d.error || 'Verification failed.', 'err');
  }
}
document.querySelectorAll('input.cred').forEach(inp => inp.addEventListener('input', () => setAuth(inp.dataset.grp, 'none')));
function toggleVis(id, btn) {
  const inp = $(id), show = inp.type === 'password';
  inp.type = show ? 'text' : 'password';
  btn.innerHTML = icon(show ? 'i-eye-off' : 'i-eye');
  btn.setAttribute('aria-label', (show ? 'Hide ' : 'Show ') + (btn.getAttribute('aria-label') || '').replace(/^(Show|Hide) /, ''));
}
function copyCredsFromExport() {
  const c = creds();
  if (!c.client_id && !c.username) { toast('No credentials on the Export tab yet.', 'warn'); return; }
  $('impClientId').value = c.client_id; $('impClientSecret').value = c.client_secret;
  $('impUsername').value = c.username;  $('impPassword').value = c.password;
  const expOk = $('expAuthPill').classList.contains('ok');
  setAuth('imp', expOk ? 'ok' : 'none', expOk ? $('expAuthNote').textContent : '');
  toast('Copied credentials from Export.', 'ok');
}
function copyMalFromExport() {
  if (!$('malUserId').value && !$('malUsername').value) { toast('No MAL profile on the Export tab yet.', 'warn'); return; }
  $('convMalId').value = $('malUserId').value; $('convMalName').value = $('malUsername').value;
  toast('Copied MAL profile from Export.', 'ok');
}

// ── Export ───────────────────────────────────────────────────────────────────
async function refreshCounts() {
  const c = creds();
  if (!haveAll(c)) { toast('Fill in all four credential fields first.', 'err'); return; }
  const btn = $('btnCounts'); btn.disabled = true; $('countsHint').textContent = 'Loading from MangaDex...';
  const d = await postJSON('/api/library_counts', c);
  btn.disabled = false;
  if (!d.ok) { $('countsHint').textContent = 'Counts load from your library'; toast(d.error || 'Could not load counts.', 'err'); return; }
  libCounts = d.counts;
  for (const [s, n] of Object.entries(d.counts)) { const el = $('count-' + s); if (el) el.textContent = n.toLocaleString(); }
  $('libTotal').hidden = false; $('libTotalN').textContent = d.total.toLocaleString();
  $('btnAllCount').textContent = `(${d.total.toLocaleString()})`;
  $('countsHint').textContent = 'Live from MangaDex';
  setAuth('exp', 'ok', $('expAuthNote').textContent || '');
  toast(`${d.total.toLocaleString()} titles in your library.`, 'ok');
}
async function startExport(status) {
  const c = creds();
  if (!haveAll(c)) { toast('Fill in all four credential fields first.', 'err'); return; }
  if (status) c.status = status;
  const d = await postJSON('/api/export', c);
  if (!d.ok) { toast(d.error || 'Could not start the export.', 'err'); return; }
  lastRunEndedAt = null;
  toast(status ? `Exporting ${status.replace(/_/g, ' ')}.` : 'Export started.', 'ok');
  pollStatus();
}
async function resumeExport() {
  const c = creds();
  if (!haveAll(c)) { toast('Fill in all four credential fields first.', 'err'); return; }
  const d = await postJSON('/api/resume', c);
  if (!d.ok) { toast(d.error || 'Could not resume.', 'err'); return; }
  lastRunEndedAt = null;
  toast('Resuming from checkpoint.', 'ok');
}
async function stopRun() {
  await postJSON('/api/stop');
  toast('Stop requested. The run halts after the current batch.', 'warn');
}

// ── Convert ──────────────────────────────────────────────────────────────────
function guessStatus(path) {
  const n = path.toLowerCase();
  if (n.includes('re_reading') || n.includes('re-reading')) return 'Reading';
  if (n.includes('reading'))   return 'Reading';
  if (n.includes('completed')) return 'Completed';
  if (n.includes('on_hold'))   return 'On-Hold';
  if (n.includes('dropped'))   return 'Dropped';
  if (n.includes('plan'))      return 'Plan to Read';
  return 'Reading';
}
function addFile() {
  const path = prompt('Full path to an exported .xlsx file:');
  if (!path) return;
  convFiles.push({ path: path.trim(), status: guessStatus(path) });
  renderFileList();
}
async function autoFill() {
  const files = await (await fetch('/api/exported_files')).json();
  if (!files.length) { toast('No exports from this session yet. Run an export first.', 'warn'); return; }
  let added = 0;
  files.forEach(path => { if (!convFiles.find(f => f.path === path)) { convFiles.push({ path, status: guessStatus(path) }); added++; } });
  renderFileList();
  if (!$('convSaveDir').value) $('convSaveDir').value = files[0].replace(/[/\\][^/\\]+$/, '');
  toast(`${added} file${added === 1 ? '' : 's'} added.`, 'ok');
}
function clearFiles() { convFiles = []; renderFileList(); }
function removeFile(i) { convFiles.splice(i, 1); renderFileList(); }
function renderFileList() {
  const el = $('fileList');
  $('convFilesPill').innerHTML = `Files <span class="val">${convFiles.length}</span>`;
  if (!convFiles.length) {
    el.innerHTML = `<div class="empty">${icon('i-sheet')}<span class="t">No files queued</span>
      <span class="s">Auto-fill picks up the .xlsx files from this session's export. Status is detected from each filename.</span>
      <div class="action-row"><button class="btn btn-sm" onclick="autoFill()">${icon('i-refresh')}Auto-fill</button><button class="btn btn-sm btn-ghost" onclick="addFile()">${icon('i-plus')}Add path</button></div></div>`;
    return;
  }
  const opts = ['Reading', 'Completed', 'On-Hold', 'Dropped', 'Plan to Read'];
  el.innerHTML = convFiles.map((f, i) => {
    const name = f.path.split(/[/\\]/).pop();
    return `<div class="row">${icon('i-sheet', 'lead')}
      <span class="ttl mono" title="${escHtml(f.path)}">${escHtml(name)}</span>
      <select aria-label="Status for ${escHtml(name)}" onchange="convFiles[${i}].status=this.value">${opts.map(s => `<option ${s === f.status ? 'selected' : ''}>${s}</option>`).join('')}</select>
      <button class="btn btn-sm btn-ghost" onclick="removeFile(${i})" aria-label="Remove ${escHtml(name)}">${icon('i-x')}</button></div>`;
  }).join('');
}
async function generateXml() {
  if (!convFiles.length) { toast('Add at least one .xlsx file first.', 'err'); return; }
  const uid = $('convMalId').value.trim(), uname = $('convMalName').value.trim();
  if (!uid || !uname) { toast('MAL user ID and username are required for Convert.', 'err'); return; }
  const btn = $('btnGenerate'); btn.disabled = true;
  $('convState').className = 'pill accent'; $('convState').innerHTML = '<i class="dot live"></i>Working';
  const d = await postJSON('/api/convert', {
    mal_user_id: uid, mal_username: uname, save_dir: $('convSaveDir').value.trim(),
    fmt_mal: $('convMal').checked, fmt_al: $('convAl').checked,
    include_scores: $('convScores').checked, dry_run: $('convDry').checked, files: convFiles,
  });
  btn.disabled = false;
  const res = $('convResult'); res.hidden = false;
  if (!d.ok) {
    $('convState').className = 'pill err'; $('convState').textContent = 'Failed';
    res.innerHTML = `<div class="callout err">${icon('i-warning')}<span><span class="ct">Convert failed</span><span class="cs">${escHtml(d.error || 'Unknown error')}</span></span></div>`;
    return;
  }
  const n = d.total.toLocaleString();
  if (d.dry) {
    $('convState').className = 'pill warn'; $('convState').textContent = 'Dry run';
    res.innerHTML = `<div class="callout warn">${icon('i-info')}<span><span class="ct">Dry run</span><span class="cs">${n} titles would be written. ${d.skipped} would be skipped.</span></span></div>`;
  } else {
    $('convState').className = 'pill ok'; $('convState').innerHTML = '<i class="dot ok"></i>Done';
    const files = (d.files || []).map(f => `<br><span class="sub-note">${escHtml(f)}</span>`).join('');
    res.innerHTML = `<div class="callout ok">${icon('i-check-circle')}<span><span class="ct">${n} titles written</span><span class="cs">${d.skipped} skipped for missing MAL IDs.${files}</span></span></div>`;
  }
  const sk = d.skipped_titles || [];
  $('convSkipPill').className = sk.length ? 'pill warn' : 'pill ok';
  $('convSkipPill').textContent = sk.length ? `${d.skipped} titles` : 'None skipped';
  if (sk.length) renderSkipRows('skippedBox', sk);
  else $('skippedBox').innerHTML = `<div class="empty">${icon('i-check-circle')}<span class="t">Nothing skipped</span><span class="s">Every title in these files has a MAL ID.</span></div>`;
}
function renderConvSkipEmpty() {
  $('skippedBox').innerHTML = `<div class="empty">${icon('i-warning')}<span class="t">No results yet</span><span class="s">Titles without a MAL link are listed here after you generate, with a quick MAL search for each.</span></div>`;
}

// ── Import ───────────────────────────────────────────────────────────────────
function setImpType(t) {
  impType = t;
  document.querySelectorAll('.seg-opt[data-imptype]').forEach(b => { const on = b.dataset.imptype === t; b.classList.toggle('active', on); b.setAttribute('aria-checked', on); });
  $('impXmlNote').hidden = t !== 'xml';
  $('impJsonNote').hidden = t !== 'json';
  $('impTypePill').innerHTML = `Source <span class="val">${t === 'xml' ? 'XML' : 'JSON'}</span>`;
  $('impFilePath').placeholder = t === 'xml' ? '/path/to/mal_reading.xml' : '/path/to/mdex_reading.json';
}
async function startImport() {
  const c = impCreds(), fp = $('impFilePath').value.trim();
  if (!haveAll(c)) { toast('Fill in all four credential fields first.', 'err'); return; }
  if (!fp) { toast('Choose a file to import.', 'err'); return; }
  const d = await postJSON('/api/import', { ...c, file_path: fp, file_type: impType,
    import_scores: $('impScores').checked, dry_run: $('impDry').checked });
  if (!d.ok) { toast(d.error || 'Could not start the import.', 'err'); return; }
  lastRunEndedAt = null;
  toast('Import started.', 'ok');
  pollStatus();
}

// ── History ──────────────────────────────────────────────────────────────────
function opOf(e) {
  const t = (e.type || '').toLowerCase();
  if (t.startsWith('import')) return 'import';
  if (t === 'convert') return 'convert';
  return 'export';
}
const STATUS_LABEL = { reading: 'Reading', completed: 'Completed', on_hold: 'On-hold', dropped: 'Dropped',
                       plan_to_read: 'Plan to read', re_reading: 'Re-reading', 'full library': 'Full library' };
function fmtDur(v) {
  const n = parseInt(v);
  if (!n) return '-';
  return n >= 60 ? `${Math.floor(n / 60)}m ${n % 60}s` : `${n}s`;
}
const OP_META = { export: ['accent', 'i-upload', 'Export'], import: ['info', 'i-download', 'Import'], convert: ['ok', 'i-convert', 'Convert'] };
async function loadHistory() {
  try { historyRows = await (await fetch('/api/history')).json(); } catch (e) { historyRows = []; }
  const runs = historyRows.length;
  const titles = historyRows.reduce((a, e) => a + (Number(e.total) || 0), 0);
  const skipped = historyRows.reduce((a, e) => a + (Number(e.skipped) || 0), 0);
  const timed = historyRows.map(e => parseInt(e.elapsed)).filter(n => n > 0);
  const avg = timed.length ? Math.round(timed.reduce((a, b) => a + b, 0) / timed.length) : 0;
  const counts = { export: 0, import: 0, convert: 0 };
  historyRows.forEach(e => counts[opOf(e)]++);

  $('statRuns').textContent = runs.toLocaleString();
  $('statRunsSub').textContent = runs ? `${counts.export} export, ${counts.import} import, ${counts.convert} convert` : 'No runs yet';
  $('statTitles').textContent = titles.toLocaleString();
  $('statTitlesSub').textContent = skipped ? `${skipped.toLocaleString()} skipped along the way` : 'Across all runs';
  $('statElapsed').innerHTML = avg ? (avg >= 60 ? `${Math.floor(avg / 60)}<small>m</small>&nbsp;${avg % 60}<small>s</small>` : `${avg}<small>s</small>`) : '-';
  $('statElapsedSub').textContent = timed.length ? `Across ${timed.length} timed runs` : 'Export and import runs';
  $('statRate').innerHTML = titles + skipped ? `${(titles / (titles + skipped) * 100).toFixed(1)}<small>%</small>` : '-';
  $('statRateSub').textContent = titles + skipped ? `${titles.toLocaleString()} of ${(titles + skipped).toLocaleString()} matched` : 'Titles not skipped';
  $('hn-all').textContent = runs;
  ['export', 'import', 'convert'].forEach(k => $('hn-' + k).textContent = counts[k]);

  $('sideLast').textContent = historyRows[0] ? historyRows[0].date.slice(5, 16) : 'Never';
  renderHistory();
}
function setHistFilter(f) {
  histFilter = f;
  document.querySelectorAll('#histTabs .tab').forEach(t => t.classList.toggle('active', t.dataset.f === f));
  renderHistory();
}
function renderHistory() {
  const q = $('historySearch').value.trim().toLowerCase();
  const rows = historyRows.filter(e => (histFilter === 'all' || opOf(e) === histFilter) &&
    (!q || [e.date, e.type, e.mode, e.files].some(v => String(v || '').toLowerCase().includes(q))));
  $('historyBody').innerHTML = rows.map(e => {
    const [cls, ic, label] = OP_META[opOf(e)];
    const raw = e.type || '';
    const scope = opOf(e) === 'import' ? raw.replace(/^Import\s*/i, '').replace(/[()]/g, '') || '-'
                : opOf(e) === 'convert' ? 'XLSX' : (STATUS_LABEL[raw.toLowerCase()] || raw || '-');
    const sk = Number(e.skipped) || 0;
    return `<tr>
      <td class="num" style="font-family:var(--mono);font-size:12px">${escHtml(e.date || '')}</td>
      <td><span class="pill ${cls}">${icon(ic)}${label}</span></td>
      <td>${escHtml(scope)}</td>
      <td class="r num" style="font-family:var(--mono)">${(Number(e.total) || 0).toLocaleString()}</td>
      <td class="r" style="font-family:var(--mono);color:${sk ? 'var(--warn)' : 'var(--faint)'}">${sk}</td>
      <td><span class="pill">${escHtml(e.mode || '-')}</span></td>
      <td class="r" style="font-family:var(--mono)">${fmtDur(e.elapsed)}</td>
      <td class="files" title="${escHtml(e.files || '')}">${escHtml(e.files || '-')}</td></tr>`;
  }).join('');
  const none = !rows.length;
  $('historyEmpty').hidden = !none;
  $('historyEmptyT').textContent = historyRows.length ? 'No matching runs' : 'No runs yet';
  $('historyEmptyS').textContent = historyRows.length ? 'Try a different filter or clear the search.' : 'Your first export, import or convert shows up here with totals, timing and output files.';
  $('histFoot').textContent = `Showing ${rows.length} of ${historyRows.length} runs`;
}
async function clearHistory() {
  if (!confirm('Clear all run history? This deletes mdex_history.json.')) return;
  await postJSON('/api/history/clear');
  toast('History cleared.', 'ok');
  loadHistory();
}

// ── Settings ─────────────────────────────────────────────────────────────────
async function loadCpInfo() {
  const d = await (await fetch('/api/checkpoint')).json();
  const has = !!d.timestamp;
  $('cpPill').className = has ? 'pill warn' : 'pill';
  $('cpPill').textContent = has ? 'Saved' : 'None';
  $('btnClearCp').disabled = !has;
  $('cpInfo').innerHTML = has
    ? `Saved <span class="num" style="color:var(--text)">${escHtml(d.timestamp.replace('T', ' ').slice(0, 19))}</span>. Finished groups: ${escHtml((d.completed || []).join(', ') || 'none yet')}.`
    : 'No checkpoint on disk. Exports start from the beginning.';
}
async function clearCheckpoint() {
  await postJSON('/api/checkpoint/clear');
  toast('Checkpoint cleared.', 'ok');
  loadCpInfo(); pollStatus();
}
async function loadInfo() {
  try {
    const d = await (await fetch('/api/info')).json();
    $('infoVer').textContent = 'v' + d.version;
    $('infoUrl').textContent = location.origin;
    $('infoCwd').textContent = d.cwd;
    $('infoHist').textContent = d.history_file;
    $('infoCp').textContent = d.checkpoint_file;
    $('infoPlat').textContent = `${d.platform}, Python ${d.python}`;
    $('brandVer').textContent = 'v' + d.version;
  } catch (e) {}
}

// ── Helpers ──────────────────────────────────────────────────────────────────
async function paste(id) {
  try {
    const d = await (await fetch('/api/clipboard')).json();
    if (d.ok && d.text) { $(id).value = d.text; $(id).dispatchEvent(new Event('input')); return; }
  } catch (e) {}
  try { $(id).value = await navigator.clipboard.readText(); $(id).dispatchEvent(new Event('input')); }
  catch (e2) { toast('Clipboard is empty or blocked. Paste manually.', 'warn'); }
}
async function browseFolder(inputId) {
  try {
    const d = await (await fetch('/api/browse_folder')).json();
    if (d.ok && d.path) { $(inputId).value = d.path; toast('Folder selected.', 'ok'); }
  } catch (e) { toast('Could not open the folder picker.', 'err'); }
}
async function browseFile(inputId) {
  try {
    const d = await (await fetch('/api/browse_file')).json();
    if (d.ok && d.path) {
      $(inputId).value = d.path;
      if (/\.json$/i.test(d.path)) setImpType('json'); else if (/\.xml$/i.test(d.path)) setImpType('xml');
      toast('File selected.', 'ok');
    }
  } catch (e) { toast('Could not open the file picker.', 'err'); }
}
let toastTimer = null;
const TOAST_ICON = { ok: 'i-check-circle', err: 'i-x', warn: 'i-warning' };
function toast(msg, type = 'ok') {
  const el = $('toast');
  el.innerHTML = icon(TOAST_ICON[type] || TOAST_ICON.ok) + `<span>${escHtml(msg)}</span>`;
  el.className = 'show ' + type;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3500);
}

// ── Keyboard ─────────────────────────────────────────────────────────────────
document.addEventListener('keydown', e => {
  const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName);
  const page = document.querySelector('.page.active').id;
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
    if (page === 'page-export' && !$('btnAll').disabled) { e.preventDefault(); startExport(null); }
    if (page === 'page-import' && !$('btnImport').disabled) { e.preventDefault(); startImport(); }
    return;
  }
  if (e.key === 'Escape' && !$('btnStop').disabled) { e.preventDefault(); stopRun(); return; }
  if (!typing && !e.ctrlKey && !e.metaKey && !e.altKey && /^[1-5]$/.test(e.key)) {
    goPage(['export', 'convert', 'import', 'history', 'settings'][Number(e.key) - 1]);
  }
});

// ── Init ─────────────────────────────────────────────────────────────────────
if (location.hash.length > 1) goPage(location.hash.slice(1));
window.addEventListener('hashchange', () => goPage(location.hash.slice(1)));
renderFileList();
renderConvSkipEmpty();
refreshHeadMeta();
loadHistory();
loadInfo();
pollStatus();
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
