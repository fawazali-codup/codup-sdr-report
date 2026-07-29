# -*- coding: utf-8 -*-
import streamlit as st
import pandas as pd
import requests
import math
import io
from datetime import datetime, date, timedelta, timezone
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── CONSTANTS ─────────────────────────────────────────────────────────────────
def _hs_token():
    return st.secrets.get("HUBSPOT_TOKEN", "")

def _hs_hdr():
    return {"Authorization": f"Bearer {_hs_token()}", "Content-Type": "application/json"}
SHEET_ID     = "1yVCqrFGQKAAv2OJQm595LPi4v7frFM6KOU8iVRXLU3U"
SOCIAL_SID   = "1r5h47A5d8BVkhapt21CVNoKpxODKb1CcZ-TSbRZNqro"
JAWWAD = "79357033"
MOHSIN = "79357044"
SDR_IDS = {JAWWAD: "Jawwad", MOHSIN: "Mohsin"}
OUTBOUND_SOURCES   = {"cold outreach", "event - pre-event list", "event - attendee list"}
EXCLUDED_SOURCES   = OUTBOUND_SOURCES | {"self prospecting", "organic", "inbound email"}
HELD_OUTCOMES      = {"COMPLETED", "QUALIFIED"}
QUALIFYING_STAGES  = {"19981364", "19981365", "contractsent", "53036612", "closedwon"}
POSITIVE_DISPOSITIONS = {"booked", "activated", "intrigued", "referred", "not now"}
BAD_DQ = {"not market fit", "no number"}

# ── WEEK PICKER ───────────────────────────────────────────────────────────────
def get_week_options(n=10):
    today = date.today()
    dow = today.weekday()  # Mon=0 … Sun=6
    # Last completed Friday (or today if today is Fri)
    days_back = (dow - 4) % 7
    last_fri = today - timedelta(days=days_back)
    weeks = []
    for i in range(n):
        friday = last_fri - timedelta(weeks=i)
        monday = friday - timedelta(days=4)
        wn = math.ceil(friday.day / 7)
        month = friday.strftime("%b")
        label = (f"Week {wn} {month}  "
                 f"({monday.strftime('%b')} {monday.day}–{friday.day}, {friday.year})")
        # ms timestamps UTC
        s_ms = int(datetime(monday.year, monday.month, monday.day, 0, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        e_ms = int(datetime(friday.year, friday.month, friday.day, 23, 59, 59, tzinfo=timezone.utc).timestamp() * 1000)
        tab  = f"Week {wn} {month}"
        weeks.append({"label": label, "tab": tab,
                      "start": monday.isoformat(), "end": friday.isoformat(),
                      "s_ms": s_ms, "e_ms": e_ms,
                      "monday": monday, "friday": friday})
    return weeks

# ── ALOWARE CSV PARSER ────────────────────────────────────────────────────────
def parse_aloware(activity_bytes, users_bytes):
    act = pd.read_csv(io.BytesIO(activity_bytes))
    usr = pd.read_csv(io.BytesIO(users_bytes))

    result = {}
    for sdr_id, name in [(JAWWAD, "Jawwad"), (MOHSIN, "Mohsin")]:
        # Activity CSV — dials + pickups
        name_lo = name.lower()
        act_row = act[act["User"].str.lower().str.replace(" ", "").str.contains(name_lo.replace(" ", ""), na=False)]
        if act_row.empty:
            act_row = act[act["User"].str.lower().str.contains(name_lo.split()[0], na=False)]

        dials = pickups = 0
        if not act_row.empty:
            r = act_row.iloc[0]
            dials = int(r.get("Number of outbound calls", r.get("Outbound Calls", 0)) or 0)
            # Pickups = all Connected* columns except Gatekeeper
            pickup_cols = [c for c in act.columns
                           if c.startswith("Connected") and "Gatekeeper" not in c]
            pickups = int(sum(int(r.get(c, 0) or 0) for c in pickup_cols))

        # Users CSV — conversations (outbound > 2 min)
        usr_row = usr[usr.apply(lambda row: name_lo.split()[0] in str(row.get("User", "")).lower()
                                or name_lo.split()[0] in str(row.get("Email", "")).lower(), axis=1)]
        conversations = 0
        if not usr_row.empty:
            r2 = usr_row.iloc[0]
            conversations = int(r2.get("Outbound calls over 2 minutes",
                                r2.get("Outbound Calls Over 2 Min", 0)) or 0)

        result[sdr_id] = {"dials": dials, "pickups": pickups, "conversations": conversations}

    return result

# ── HUBSPOT HELPERS ───────────────────────────────────────────────────────────
def hs_post(url, payload):
    r = requests.post(url, headers=_hs_hdr(), json=payload)
    r.raise_for_status()
    return r.json()

def hs_get(url, params=None):
    r = requests.get(url, headers=_hs_hdr(), params=params)
    r.raise_for_status()
    return r.json()

def search_all(url, payload):
    results, after = [], None
    p = dict(payload)
    while True:
        if after:
            p["after"] = after
        data = hs_post(url, p)
        results.extend(data.get("results", []))
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return results

def get_contact(cid):
    return hs_get(
        f"https://api.hubapi.com/crm/v3/objects/contacts/{cid}",
        params={"properties": "firstname,lastname,sdr,sdr_source,hs_lead_status"}
    ).get("properties", {})

def get_meeting_contacts(mid):
    assoc = hs_get(f"https://api.hubapi.com/crm/v3/objects/meetings/{mid}/associations/contacts")
    return [get_contact(a["id"]) for a in assoc.get("results", [])]

# ── DATA PULL FUNCTIONS ───────────────────────────────────────────────────────
def pull_completions(start_date, end_date):
    comps = {JAWWAD: [], MOHSIN: []}
    pos   = {JAWWAD: [], MOHSIN: []}
    for sdr_id in [JAWWAD, MOHSIN]:
        all_c = search_all(
            "https://api.hubapi.com/crm/v3/objects/contacts/search",
            {"filterGroups": [{"filters": [
                {"propertyName": "sdr",         "operator": "EQ",           "value": sdr_id},
                {"propertyName": "completion",  "operator": "HAS_PROPERTY"},
                {"propertyName": "completions", "operator": "EQ",           "value": "true"},
            ]}],
            "properties": ["firstname","lastname","completion","completion_date","sdr_source"],
            "limit": 100}
        )
        week = [c for c in all_c
                if start_date <= (c["properties"].get("completion_date") or "") <= end_date
                and (c["properties"].get("sdr_source") or "").lower() in OUTBOUND_SOURCES]
        comps[sdr_id] = week
        pos[sdr_id]   = [c for c in week
                         if (c["properties"].get("completion") or "").lower() in POSITIVE_DISPOSITIONS]
    return comps, pos

def pull_meetings_booked_outbound(start_date, end_date):
    out = {JAWWAD: [], MOHSIN: []}
    for sdr_id in [JAWWAD, MOHSIN]:
        all_c = search_all(
            "https://api.hubapi.com/crm/v3/objects/contacts/search",
            {"filterGroups": [{"filters": [
                {"propertyName": "sdr",         "operator": "EQ", "value": sdr_id},
                {"propertyName": "completion",  "operator": "EQ", "value": "Booked"},
                {"propertyName": "completions", "operator": "EQ", "value": "true"},
            ]}],
            "properties": ["firstname","lastname","completion_date","sdr_source"],
            "limit": 100}
        )
        out[sdr_id] = [c for c in all_c
                       if start_date <= (c["properties"].get("completion_date") or "") <= end_date
                       and (c["properties"].get("sdr_source") or "").lower() in OUTBOUND_SOURCES]
    return out

def pull_meeting_objects(s_ms, e_ms, progress_cb=None):
    all_mtgs = search_all(
        "https://api.hubapi.com/crm/v3/objects/meetings/search",
        {"filterGroups": [{"filters": [
            {"propertyName": "hs_meeting_start_time", "operator": "GTE", "value": str(s_ms)},
            {"propertyName": "hs_meeting_start_time", "operator": "LTE", "value": str(e_ms)},
        ]}],
        "properties": ["hs_meeting_title","hs_meeting_start_time","hs_meeting_outcome","hs_activity_type"],
        "limit": 50}
    )

    self_prosp = {JAWWAD: [], MOHSIN: []}
    inbound    = {JAWWAD: [], MOHSIN: []}
    confirmed  = {JAWWAD: [], MOHSIN: []}
    held       = {JAWWAD: [], MOHSIN: []}

    for idx, m in enumerate(all_mtgs):
        if progress_cb:
            progress_cb(idx, len(all_mtgs))
        p           = m["properties"]
        title       = p.get("hs_meeting_title", "(no title)")
        outcome     = p.get("hs_meeting_outcome", "") or ""
        mtype       = p.get("hs_activity_type", "") or ""
        ts          = str(p.get("hs_meeting_start_time", ""))[:10]

        if mtype != "Intro Meeting":
            continue

        contacts = get_meeting_contacts(m["id"])
        for cp in contacts:
            sdr_id     = cp.get("sdr", "")
            if sdr_id not in SDR_IDS:
                continue
            source     = (cp.get("sdr_source") or "").strip()
            source_low = source.lower()
            lead_st    = (cp.get("hs_lead_status") or "").lower()
            cname      = f"{cp.get('firstname','')} {cp.get('lastname','')}".strip()
            entry = {"title": title, "date": ts, "contact": cname,
                     "source": source, "outcome": outcome}

            if not source:
                continue

            if source_low == "self prospecting":
                self_prosp[sdr_id].append(entry)
            elif source_low in ("organic",):
                if lead_st == "booked" and outcome in HELD_OUTCOMES:
                    confirmed[sdr_id].append(entry)
            elif source_low == "inbound email":
                inbound[sdr_id].append(entry)
            elif source_low not in EXCLUDED_SOURCES:
                inbound[sdr_id].append(entry)

            if outcome in HELD_OUTCOMES and source_low in OUTBOUND_SOURCES:
                held[sdr_id].append(entry)

    # dedup each bucket
    def dedup(d):
        out = {JAWWAD: [], MOHSIN: []}
        for sdr_id in [JAWWAD, MOHSIN]:
            seen = set()
            for e in d[sdr_id]:
                k = (e["title"], e["date"], e["contact"])
                if k not in seen:
                    seen.add(k)
                    out[sdr_id].append(e)
        return out

    return dedup(self_prosp), dedup(inbound), dedup(confirmed), dedup(held)

def pull_pipeline(s_ms, e_ms):
    all_deals = search_all(
        "https://api.hubapi.com/crm/v3/objects/deals/search",
        {"filterGroups": [{"filters": [
            {"propertyName": "hs_createdate", "operator": "GTE", "value": str(s_ms)},
            {"propertyName": "hs_createdate", "operator": "LTE", "value": str(e_ms)},
        ]}],
        "properties": ["dealname","dealstage","amount","hs_createdate"],
        "limit": 100}
    )
    pipeline = {JAWWAD: [], MOHSIN: []}
    for deal in all_deals:
        p     = deal["properties"]
        stage = p.get("dealstage", "")
        if stage not in QUALIFYING_STAGES:
            continue
        amt   = float(p.get("amount") or 0)
        dname = p.get("dealname", "(no name)")
        assoc = hs_get(f"https://api.hubapi.com/crm/v3/objects/deals/{deal['id']}/associations/contacts")
        for a in assoc.get("results", []):
            cp = hs_get(f"https://api.hubapi.com/crm/v3/objects/contacts/{a['id']}",
                        params={"properties": "firstname,lastname,sdr,sdr_source"}).get("properties", {})
            sdr_id = cp.get("sdr", "")
            if sdr_id in SDR_IDS and (cp.get("sdr_source") or "").lower() in OUTBOUND_SOURCES:
                pipeline[sdr_id].append({
                    "deal": dname, "amount": amt, "stage": stage,
                    "contact": f"{cp.get('firstname','')} {cp.get('lastname','')}".strip()
                })
    return pipeline

def pull_email_volume(s_ms, e_ms):
    counts = {JAWWAD: 0, MOHSIN: 0}
    for sdr_id in [JAWWAD, MOHSIN]:
        emails = search_all(
            "https://api.hubapi.com/crm/v3/objects/emails/search",
            {"filterGroups": [{"filters": [
                {"propertyName": "hs_email_direction",  "operator": "EQ", "value": "EMAIL"},
                {"propertyName": "hubspot_owner_id",    "operator": "EQ", "value": sdr_id},
                {"propertyName": "hs_createdate",       "operator": "GTE","value": str(s_ms)},
                {"propertyName": "hs_createdate",       "operator": "LTE","value": str(e_ms)},
            ]}],
            "properties": ["hs_email_direction","hubspot_owner_id"],
            "limit": 100}
        )
        counts[sdr_id] = len(emails)
    return counts

def pull_batch_error_rate(s_ms, e_ms):
    all_c = search_all(
        "https://api.hubapi.com/crm/v3/objects/contacts/search",
        {"filterGroups": [{"filters": [
            {"propertyName": "createdate", "operator": "GTE", "value": str(s_ms)},
            {"propertyName": "createdate", "operator": "LTE", "value": str(e_ms)},
        ]}],
        "properties": ["createdate","data_quality"],
        "limit": 100}
    )
    total = len(all_c)
    bad   = sum(1 for c in all_c
                if (c["properties"].get("data_quality") or "").lower() in BAD_DQ)
    return bad, total

def pull_social_prospecting(monday, friday):
    creds = service_account.Credentials.from_service_account_info(
        st.secrets["gcp_service_account"],
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    svc     = build("sheets", "v4", credentials=creds)
    meta    = svc.spreadsheets().get(spreadsheetId=SOCIAL_SID).execute()
    tabs    = [s["properties"]["title"] for s in meta["sheets"]]

    # Find tab: look for month name + start day
    month_name = monday.strftime("%B")
    start_day  = str(monday.day)
    target = next((t for t in tabs
                   if month_name.lower() in t.lower() and start_day in t), None)
    if not target:
        # fallback: just month + year
        target = next((t for t in tabs
                       if month_name.lower() in t.lower()), None)

    counts = {JAWWAD: 0, MOHSIN: 0}
    if not target:
        return counts, None

    rows = svc.spreadsheets().values().get(
        spreadsheetId=SOCIAL_SID, range=f"'{target}'!A:E"
    ).execute().get("values", [])

    for r in rows[1:]:
        if len(r) < 3 or not (r[2].strip() if len(r) > 2 else ""):
            continue
        sdr_col = (r[1] if len(r) > 1 else "").lower()
        if "jawwad" in sdr_col:
            counts[JAWWAD] += 1
        elif "mohsin" in sdr_col:
            counts[MOHSIN] += 1

    return counts, target

# ── THRESHOLD COLORS ──────────────────────────────────────────────────────────
def color(key, val):
    """Returns 🟢 / 🟡 / 🔴 based on metric thresholds. Returns '' for reading metrics."""
    T = {
        # (green_min, yellow_min)  — value >= green_min → green, >= yellow_min → yellow, else red
        "dials":              (945,  840),
        "p2c_pct":            (27,   24),
        "email":              (252,  224),
        "social":             (45,   40),
        "completions":        (49,   43),
        "pos_completions":    (23,   20),
        "mbo":                (2,    2),   # booked outbound: >=2 green, 1 yellow, 0 red
        "mbsp":               (1,    1),   # self-prosp: >=1 green, 0 red
        "held":               (2,    1),
        "pipeline":           (45000, 40000),
        "accounts":           (36,   32),
        "contacts":           (270,  240),
        # lower-is-better
        "enrich_pct":         None,   # reading
        "batch_err_pct":      None,   # lower is better — handled separately
    }
    if key == "batch_err_pct":
        try:
            v = float(str(val).replace("%",""))
            return "🟢" if v <= 5 else ("🟡" if v <= 10 else "🔴")
        except:
            return ""
    if key == "enrich_pct":
        try:
            v = float(str(val).replace("%",""))
            return "🟢" if v >= 13.5 else ("🟡" if v >= 12 else "🔴")
        except:
            return ""
    if key not in T or T[key] is None:
        return ""
    g, y = T[key]
    try:
        v = float(str(val).replace("$","").replace(",","").replace("%",""))
        return "🟢" if v >= g else ("🟡" if v >= y else "🔴")
    except:
        return ""

# ── GOOGLE SHEETS WRITER ──────────────────────────────────────────────────────
def build_sheets_service():
    creds = service_account.Credentials.from_service_account_info(
        st.secrets["gcp_service_account"],
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=creds)

def tab_exists(svc, tab_name):
    meta = svc.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    return next((s for s in meta["sheets"] if s["properties"]["title"] == tab_name), None)

def delete_tab(svc, sheet_id_num):
    svc.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={
        "requests": [{"deleteSheet": {"sheetId": sheet_id_num}}]
    }).execute()

def create_tab(svc, tab_name):
    svc.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={
        "requests": [{"addSheet": {"properties": {"title": tab_name}}}]
    }).execute()
    meta = svc.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    return next(s["properties"]["sheetId"] for s in meta["sheets"]
                if s["properties"]["title"] == tab_name)

def write_kpi_sheet(tab_name, d, week_label, monday, friday):
    """d = merged data dict with all metric values."""
    svc = build_sheets_service()

    def pct(n, tot):
        return f"{n/tot*100:.1f}%" if tot else "N/A"

    def fmt_amt(a):
        return f"${a:,.0f}" if a else "$0"

    j = JAWWAD; m = MOHSIN

    # ── compute derived values ─────────────────────────────────
    j_dials = d["aloware"][j]["dials"]; m_dials = d["aloware"][m]["dials"]
    j_pick  = d["aloware"][j]["pickups"]; m_pick  = d["aloware"][m]["pickups"]
    j_conv  = d["aloware"][j]["conversations"]; m_conv  = d["aloware"][m]["conversations"]
    t_dials = j_dials + m_dials
    t_pick  = j_pick  + m_pick
    t_conv  = j_conv  + m_conv
    j_p2c   = pct(j_conv, j_pick); m_p2c = pct(m_conv, m_pick); t_p2c = pct(t_conv, t_pick)

    j_email = d["email"][j]; m_email = d["email"][m]; t_email = j_email + m_email
    j_soc   = d["social"][j]; m_soc   = d["social"][m]; t_soc   = j_soc + m_soc

    j_comp  = d["completions_count"][j]; m_comp = d["completions_count"][m]
    j_pos   = d["pos_count"][j];         m_pos  = d["pos_count"][m]
    t_comp  = j_comp + m_comp;           t_pos  = j_pos + m_pos
    j_cr    = pct(j_comp, j_dials); m_cr = pct(m_comp, m_dials); t_cr = pct(t_comp, t_dials)
    j_pr    = pct(j_pos, j_comp);   m_pr = pct(m_pos, m_comp);   t_pr = pct(t_pos, t_comp)

    j_mbo   = d["mbo_count"][j]; m_mbo = d["mbo_count"][m]; t_mbo = j_mbo + m_mbo
    j_mbsp  = d["mbsp_count"][j];m_mbsp= d["mbsp_count"][m]; t_mbsp= j_mbsp+ m_mbsp
    j_mbi   = d["mbi_count"][j]; m_mbi = d["mbi_count"][m]; t_mbi = j_mbi + m_mbi
    j_conf  = d["conf_count"][j];m_conf = d["conf_count"][m]; t_conf= j_conf + m_conf
    j_held  = d["held_count"][j];m_held = d["held_count"][m]; t_held= j_held + m_held

    j_pipe  = sum(e["amount"] for e in d["pipeline"][j])
    m_pipe  = sum(e["amount"] for e in d["pipeline"][m])
    t_pipe  = j_pipe + m_pipe

    b_err, b_tot = d["batch_bad"], d["batch_total"]
    ber_pct  = f"{b_err/b_tot*100:.1f}%" if b_tot else "N/A"
    accounts = str(d["accounts"]); contacts_n = str(d["contacts"])
    # enrichment quality: pickups / dials (connect rate)
    eq_pct = pct(t_pick, t_dials) if t_dials else "N/A"

    date_range = f"{monday.strftime('%B')} {monday.day}–{friday.day}, {friday.year}"

    G = "🟢 Green"; Y = "🟡 Yellow"; R = "🔴 Red"

    rows = [
        [f"{tab_name} — SDR KPI Report | {date_range}"],
        [],
        ["SALES SUPPORT"],
        ["Metric", "Numbers", "Weekly Goal", G, Y, R],
        ["New Accounts Prospected",           accounts,    ">= 40",   ">= 36",    "32-35",    "< 32"],
        ["New Contacts Pulled",               contacts_n,  ">= 300",  ">= 270",   "240-269",  "< 240"],
        ["Enrichment Quality (Connect Rate)", eq_pct,      ">= 15%",  ">= 13.5%", "12-13.4%", "< 12%"],
        ["Batch Error Rate",                  ber_pct,     "< 5%",    "0-5%",     "6-10%",    "> 10%"],
        [],
        ["SDR TEAM ROLLUP"],
        ["Metric", "Numbers", "Weekly Goal", G, Y, R],
        ["Total Dials",                        str(t_dials), ">= 1,050","945+",     "840-944",  "< 840"],
        ["Pickups",                            str(t_pick),  "—","—","—","— (reading)"],
        ["Conversations",                      str(t_conv),  "—","—","—","— (reading)"],
        ["Pickup-to-Conversation Ratio",       t_p2c,        ">= 30%",  ">= 27%",  "24-26.9%", "< 24%"],
        ["Email Volume",                       str(t_email), ">= 280",  "252+",     "224-251",  "< 224"],
        ["Social Prospecting Reach Out",       str(t_soc),   ">= 50",   "45+",      "40-44",    "< 40"],
        ["Completions",                        str(t_comp),  ">= 54",   "49+",      "43-48",    "< 43"],
        ["Completion Ratio",                   t_cr,         "—","—","—","— (reading)"],
        ["Positive Completions",               str(t_pos),   ">= 25",   "23+",      "20-22",    "< 20"],
        ["Positive Completions Ratio",         t_pr,         "—","—","—","— (reading)"],
        ["Meetings Booked (Outbound)",         str(t_mbo),   ">= 2",    "2+",       "1",        "0"],
        ["Meetings Booked (Self-Prospecting)", str(t_mbsp),  ">= 1",    "1+",       "—",        "0"],
        ["Meetings Booked (Inbound)",          str(t_mbi),   "—","—","—","— (reading)"],
        ["Meetings Confirmed (Inbound)",       str(t_conf),  "—","—","—","— (reading)"],
        ["Meetings Held",                      str(t_held),  ">= 2",    "60-70% show rate","50%","40%"],
        ["New Qualified Pipeline",             fmt_amt(t_pipe), "$50,000","—","—","—"],
        [],
        ["JAWWAD RASOOL"],
        ["Metric", "Value"],
        ["Total Dials",                        str(j_dials)],
        ["Pickups",                            str(j_pick)],
        ["Conversations",                      str(j_conv)],
        ["Pickup-to-Conversation Ratio",       j_p2c],
        ["Email Volume",                       str(j_email)],
        ["Social Prospecting Reach Out",       str(j_soc)],
        ["Completions",                        str(j_comp)],
        ["Completion Ratio",                   j_cr],
        ["Positive Completions",               str(j_pos)],
        ["Positive Completions Ratio",         j_pr],
        ["Meetings Booked (Outbound)",         str(j_mbo)],
        ["Meetings Booked (Self-Prospecting)", str(j_mbsp)],
        ["Meetings Booked (Inbound)",          str(j_mbi)],
        ["Meetings Confirmed (Inbound)",       str(j_conf)],
        ["Meetings Held",                      str(j_held)],
        ["New Qualified Pipeline",             fmt_amt(j_pipe)],
        [],
        ["MOHSIN ALI KHAN"],
        ["Metric", "Value"],
        ["Total Dials",                        str(m_dials)],
        ["Pickups",                            str(m_pick)],
        ["Conversations",                      str(m_conv)],
        ["Pickup-to-Conversation Ratio",       m_p2c],
        ["Email Volume",                       str(m_email)],
        ["Social Prospecting Reach Out",       str(m_soc)],
        ["Completions",                        str(m_comp)],
        ["Completion Ratio",                   m_cr],
        ["Positive Completions",               str(m_pos)],
        ["Positive Completions Ratio",         m_pr],
        ["Meetings Booked (Outbound)",         str(m_mbo)],
        ["Meetings Booked (Self-Prospecting)", str(m_mbsp)],
        ["Meetings Booked (Inbound)",          str(m_mbi)],
        ["Meetings Confirmed (Inbound)",       str(m_conf)],
        ["Meetings Held",                      str(m_held)],
        ["New Qualified Pipeline",             fmt_amt(m_pipe)],
    ]

    svc.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"'{tab_name}'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": rows}
    ).execute()

    # ── FORMATTING ─────────────────────────────────────────────
    GREEN_C  = {"red": 0.714, "green": 0.843, "blue": 0.659}
    YELLOW_C = {"red": 1.0,   "green": 0.898, "blue": 0.600}
    RED_C    = {"red": 0.918, "green": 0.600, "blue": 0.600}
    SECT_BG  = {"red": 0.267, "green": 0.329, "blue": 0.416}
    SECT_FG  = {"red": 1.0,   "green": 1.0,   "blue": 1.0  }
    CHDRBG   = {"red": 0.851, "green": 0.886, "blue": 0.953}
    TITLEBG  = {"red": 0.173, "green": 0.243, "blue": 0.314}
    TITLEFG  = {"red": 1.0,   "green": 1.0,   "blue": 1.0  }

    sid = tab_exists(svc, tab_name)["properties"]["sheetId"]

    def fmt_cell(r, c, bg, bold=True):
        return {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": r, "endRowIndex": r+1,
                      "startColumnIndex": c, "endColumnIndex": c+1},
            "cell": {"userEnteredFormat": {"backgroundColor": bg,
                                           "textFormat": {"bold": bold}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"
        }}

    def fmt_row(r, c0, c1, bg=None, fg=None, bold=False, sz=None):
        tf = {}
        if fg:   tf["foregroundColor"] = fg
        if bold: tf["bold"] = True
        if sz:   tf["fontSize"] = sz
        uf = {}
        if bg: uf["backgroundColor"] = bg
        if tf: uf["textFormat"] = tf
        return {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": r, "endRowIndex": r+1,
                      "startColumnIndex": c0, "endColumnIndex": c1},
            "cell": {"userEnteredFormat": uf},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"
        }}

    def merge(r, c0, c1):
        return {"mergeCells": {
            "range": {"sheetId": sid, "startRowIndex": r, "endRowIndex": r+1,
                      "startColumnIndex": c0, "endColumnIndex": c1},
            "mergeType": "MERGE_ALL"
        }}

    # Map metric → color key
    rollup_map = {
        11: color("dials",           t_dials),
        14: color("p2c_pct",         float(t_p2c.replace("%","")) if t_p2c != "N/A" else 0),
        15: color("email",           t_email),
        16: color("social",          t_soc),
        17: color("completions",     t_comp),
        19: color("pos_completions", t_pos),
        21: color("mbo",             t_mbo),
        22: color("mbsp",            t_mbsp),
        25: color("held",            t_held),
        26: color("pipeline",        t_pipe),
    }
    COLOR_MAP = {"🟢": GREEN_C, "🟡": YELLOW_C, "🔴": RED_C}

    ss_map = {
        4: color("accounts",    int(accounts)),
        5: color("contacts",    int(contacts_n)),
        6: color("enrich_pct",  eq_pct),
        7: color("batch_err_pct", ber_pct),
    }

    reqs = []
    reqs += [fmt_row(0, 0, 8, bg=TITLEBG, fg=TITLEFG, bold=True, sz=12), merge(0, 0, 8)]
    for r in [2, 9, 29, 49]:
        reqs += [fmt_row(r, 0, 8, bg=SECT_BG, fg=SECT_FG, bold=True), merge(r, 0, 8)]
    for r in [3, 10, 30, 50]:
        reqs.append(fmt_row(r, 0, 8, bg=CHDRBG, bold=True))
    for row, c_emoji in ss_map.items():
        c = COLOR_MAP.get(c_emoji)
        if c:
            reqs.append(fmt_cell(row, 1, c))
    for row, c_emoji in rollup_map.items():
        c = COLOR_MAP.get(c_emoji)
        if c:
            reqs.append(fmt_cell(row, 1, c))
    reqs.append({"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
        "properties": {"pixelSize": 290}, "fields": "pixelSize"}})
    reqs.append({"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 7},
        "properties": {"pixelSize": 140}, "fields": "pixelSize"}})

    svc.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={"requests": reqs}).execute()
    return sid

# ── STREAMLIT UI ──────────────────────────────────────────────────────────────
st.set_page_config(page_title="SDR Weekly KPI Report", page_icon="📊", layout="wide")
st.title("📊 SDR Weekly KPI Report")

# ── STEP 1: WEEK + INPUTS ─────────────────────────────────────────────────────
st.header("1  — Week & Inputs")

weeks = get_week_options(10)
week_labels = [w["label"] for w in weeks]
sel_idx = st.selectbox("Select reporting week", range(len(week_labels)),
                       format_func=lambda i: week_labels[i], index=0)
week = weeks[sel_idx]

col1, col2 = st.columns(2)
with col1:
    act_file = st.file_uploader("Aloware Activity CSV", type="csv", key="act")
with col2:
    usr_file = st.file_uploader("Aloware Users CSV", type="csv", key="usr")

col3, col4 = st.columns(2)
with col3:
    contacts_n = st.number_input("New Contacts Pulled (Sales Support)", min_value=0, value=0, step=1)
with col4:
    accounts_n = st.number_input("New Accounts Prospected (Sales Support)", min_value=0, value=0, step=1)

run_btn = st.button("▶  Run Report", type="primary",
                    disabled=(act_file is None or usr_file is None))

if act_file is None or usr_file is None:
    st.info("Upload both Aloware CSVs to enable the Run button.")

# ── STEP 2: RUN ───────────────────────────────────────────────────────────────
if run_btn:
    st.session_state.pop("results", None)
    st.session_state.pop("overrides", None)

    progress_bar = st.progress(0, text="Starting...")

    def upd(pct, msg):
        progress_bar.progress(pct, text=msg)

    upd(2, "Parsing Aloware CSVs…")
    aloware = parse_aloware(act_file.read(), usr_file.read())

    upd(8, "Pulling completions from HubSpot…")
    comps_raw, pos_raw = pull_completions(week["start"], week["end"])

    upd(20, "Pulling meetings booked (outbound) from HubSpot…")
    mbo_raw = pull_meetings_booked_outbound(week["start"], week["end"])

    upd(30, "Fetching meeting objects — this can take a minute…")
    total_mtgs_est = 20  # rough estimate
    def mtg_progress(idx, total):
        pct = 30 + int((idx / max(total, 1)) * 30)
        upd(pct, f"Processing meeting {idx+1}/{total}…")
    self_prosp_raw, inbound_raw, confirmed_raw, held_raw = pull_meeting_objects(
        week["s_ms"], week["e_ms"], progress_cb=mtg_progress)

    upd(62, "Pulling pipeline deals…")
    pipeline_raw = pull_pipeline(week["s_ms"], week["e_ms"])

    upd(74, "Pulling email volume…")
    email_raw = pull_email_volume(week["s_ms"], week["e_ms"])

    upd(82, "Pulling batch error rate…")
    batch_bad, batch_total = pull_batch_error_rate(week["s_ms"], week["e_ms"])

    upd(90, "Pulling social prospecting…")
    social_raw, social_tab = pull_social_prospecting(week["monday"], week["friday"])

    upd(100, "Done!")
    progress_bar.empty()

    # Store raw results in session state
    st.session_state["results"] = {
        "week":       week,
        "contacts":   contacts_n,
        "accounts":   accounts_n,
        "aloware":    aloware,
        "completions": comps_raw,
        "pos":         pos_raw,
        "mbo":         mbo_raw,
        "self_prosp":  self_prosp_raw,
        "inbound":     inbound_raw,
        "confirmed":   confirmed_raw,
        "held":        held_raw,
        "pipeline":    pipeline_raw,
        "email":       email_raw,
        "batch_bad":   batch_bad,
        "batch_total": batch_total,
        "social":      social_raw,
        "social_tab":  social_tab,
    }
    # Counts (overrideable)
    st.session_state["overrides"] = {
        "completions_count": {JAWWAD: len(comps_raw[JAWWAD]),   MOHSIN: len(comps_raw[MOHSIN])},
        "pos_count":         {JAWWAD: len(pos_raw[JAWWAD]),     MOHSIN: len(pos_raw[MOHSIN])},
        "mbo_count":         {JAWWAD: len(mbo_raw[JAWWAD]),     MOHSIN: len(mbo_raw[MOHSIN])},
        "mbsp_count":        {JAWWAD: len(self_prosp_raw[JAWWAD]),MOHSIN: len(self_prosp_raw[MOHSIN])},
        "mbi_count":         {JAWWAD: len(inbound_raw[JAWWAD]), MOHSIN: len(inbound_raw[MOHSIN])},
        "conf_count":        {JAWWAD: len(confirmed_raw[JAWWAD]),MOHSIN: len(confirmed_raw[MOHSIN])},
        "held_count":        {JAWWAD: len(held_raw[JAWWAD]),    MOHSIN: len(held_raw[MOHSIN])},
        "pipeline":          pipeline_raw,
        "aloware":           aloware,
        "email":             {JAWWAD: email_raw[JAWWAD], MOHSIN: email_raw[MOHSIN]},
        "social":            {JAWWAD: social_raw[JAWWAD], MOHSIN: social_raw[MOHSIN]},
        "batch_bad":         batch_bad,
        "batch_total":       batch_total,
        "contacts":          contacts_n,
        "accounts":          accounts_n,
    }
    st.rerun()

# ── STEP 3: RESULTS ───────────────────────────────────────────────────────────
if "results" in st.session_state:
    R = st.session_state["results"]
    OV = st.session_state["overrides"]
    week = R["week"]

    # Sync any override widget values into OV before computing rollup or per-SDR tables.
    # Widget states are set on previous renders; reading them here ensures both the
    # rollup (above the inputs) and per-SDR sections (below) use the same updated values.
    _ov_keys = ["completions_count", "pos_count", "mbo_count", "mbsp_count",
                "mbi_count", "conf_count", "held_count"]
    for _k in _ov_keys:
        if f"ov_{_k}_j" in st.session_state:
            OV[_k][JAWWAD] = st.session_state[f"ov_{_k}_j"]
        if f"ov_{_k}_m" in st.session_state:
            OV[_k][MOHSIN] = st.session_state[f"ov_{_k}_m"]

    st.header("2  — Review Results")
    st.caption(f"Week: **{week['tab']}** ({week['start']} to {week['end']})")

    def leads_table(entries, columns=None):
        if not entries:
            st.caption("_No entries._")
            return
        cols = columns or ["contact", "source", "date", "outcome"]
        df = pd.DataFrame(entries)[cols]
        st.dataframe(df, use_container_width=True, hide_index=True)

    def comp_table(entries):
        if not entries:
            st.caption("_No entries._")
            return
        rows = [{"Contact": f"{c['properties'].get('firstname','')} {c['properties'].get('lastname','')}".strip(),
                 "Disposition": c["properties"].get("completion",""),
                 "Date": c["properties"].get("completion_date",""),
                 "SDR Source": c["properties"].get("sdr_source","")}
                for c in entries]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    def mbo_table(entries):
        if not entries:
            st.caption("_No entries._")
            return
        rows = [{"Contact": f"{c['properties'].get('firstname','')} {c['properties'].get('lastname','')}".strip(),
                 "SDR Source": c["properties"].get("sdr_source",""),
                 "Completion Date": c["properties"].get("completion_date","")}
                for c in entries]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    def pipeline_table(entries):
        if not entries:
            st.caption("_No entries._")
            return
        rows = [{"Deal": e["deal"], "Amount": f"${e['amount']:,.0f}",
                 "Stage": e["stage"], "Contact": e["contact"]}
                for e in entries]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # ── SALES SUPPORT ──────────────────────────────────────────
    st.subheader("Sales Support")
    b_bad = OV["batch_bad"]; b_tot = OV["batch_total"]
    t_pick = OV["aloware"][JAWWAD]["pickups"] + OV["aloware"][MOHSIN]["pickups"]
    t_dials_eq = OV["aloware"][JAWWAD]["dials"] + OV["aloware"][MOHSIN]["dials"]
    ber = f"{b_bad/b_tot*100:.1f}%" if b_tot else "N/A"
    eq  = f"{t_pick/t_dials_eq*100:.1f}%" if t_dials_eq else "N/A"

    ss_data = pd.DataFrame([
        {"Metric": "New Accounts Prospected",           "Value": str(OV["accounts"]),  "Status": color("accounts",     OV["accounts"])},
        {"Metric": "New Contacts Pulled",               "Value": str(OV["contacts"]),  "Status": color("contacts",     OV["contacts"])},
        {"Metric": "Enrichment Quality (Connect Rate)", "Value": eq,                   "Status": color("enrich_pct",   eq)},
        {"Metric": "Batch Error Rate",                  "Value": ber,                  "Status": color("batch_err_pct",ber)},
    ])
    st.dataframe(ss_data, use_container_width=True, hide_index=True,
                 column_config={"Status": st.column_config.TextColumn(width="small")})

    # ── TEAM ROLLUP ────────────────────────────────────────────
    st.subheader("SDR Team Rollup")

    def _pct(n, d): return f"{n/d*100:.1f}%" if d else "N/A"
    def _v(k): return OV["aloware"][JAWWAD][k] + OV["aloware"][MOHSIN][k]

    t_dials = _v("dials"); t_pick2 = _v("pickups"); t_conv = _v("conversations")
    t_email = OV["email"][JAWWAD] + OV["email"][MOHSIN]
    t_soc   = OV["social"][JAWWAD] + OV["social"][MOHSIN]
    t_comp  = OV["completions_count"][JAWWAD] + OV["completions_count"][MOHSIN]
    t_pos   = OV["pos_count"][JAWWAD] + OV["pos_count"][MOHSIN]
    t_mbo   = OV["mbo_count"][JAWWAD] + OV["mbo_count"][MOHSIN]
    t_mbsp  = OV["mbsp_count"][JAWWAD] + OV["mbsp_count"][MOHSIN]
    t_mbi   = OV["mbi_count"][JAWWAD] + OV["mbi_count"][MOHSIN]
    t_conf  = OV["conf_count"][JAWWAD] + OV["conf_count"][MOHSIN]
    t_held  = OV["held_count"][JAWWAD] + OV["held_count"][MOHSIN]
    j_pipe_amt = sum(e["amount"] for e in OV["pipeline"][JAWWAD])
    m_pipe_amt = sum(e["amount"] for e in OV["pipeline"][MOHSIN])
    t_pipe  = j_pipe_amt + m_pipe_amt
    rollup_rows = [
        ("Total Dials",                        str(t_dials),  color("dials",t_dials)),
        ("Pickups",                            str(t_pick2),  ""),
        ("Conversations",                      str(t_conv),   ""),
        ("Pickup-to-Conversation Ratio",       _pct(t_conv,t_pick2), color("p2c_pct", float(_pct(t_conv,t_pick2).replace("%","")) if _pct(t_conv,t_pick2) != "N/A" else 0)),
        ("Email Volume",                       str(t_email),  color("email",t_email)),
        ("Social Prospecting Reach Out",       str(t_soc),    color("social",t_soc)),
        ("Completions",                        str(t_comp),   color("completions",t_comp)),
        ("Completion Ratio",                   _pct(t_comp,t_dials), ""),
        ("Positive Completions",               str(t_pos),    color("pos_completions",t_pos)),
        ("Positive Completions Ratio",         _pct(t_pos,t_comp), ""),
        ("Meetings Booked (Outbound)",         str(t_mbo),    color("mbo",t_mbo)),
        ("Meetings Booked (Self-Prospecting)", str(t_mbsp),   color("mbsp",t_mbsp)),
        ("Meetings Booked (Inbound)",          str(t_mbi),    ""),
        ("Meetings Confirmed (Inbound)",       str(t_conf),   ""),
        ("Meetings Held",                      str(t_held),   color("held",t_held)),
        ("New Qualified Pipeline",             f"${t_pipe:,.0f}", color("pipeline",t_pipe)),
    ]
    st.dataframe(
        pd.DataFrame(rollup_rows, columns=["Metric","Value","Status"]),
        use_container_width=True, hide_index=True,
        column_config={"Status": st.column_config.TextColumn(width="small")}
    )

    # ── PER-SDR + LEAD DETAILS ─────────────────────────────────
    for sdr_id, sdr_name in [(JAWWAD, "Jawwad Rasool"), (MOHSIN, "Mohsin Ali Khan")]:
        st.subheader(sdr_name)
        j_dials2 = OV["aloware"][sdr_id]["dials"]
        j_pick2  = OV["aloware"][sdr_id]["pickups"]
        j_conv2  = OV["aloware"][sdr_id]["conversations"]
        j_email2 = OV["email"][sdr_id]
        j_soc2   = OV["social"][sdr_id]
        j_comp2  = OV["completions_count"][sdr_id]
        j_pos2   = OV["pos_count"][sdr_id]
        j_mbo2   = OV["mbo_count"][sdr_id]
        j_mbsp2  = OV["mbsp_count"][sdr_id]
        j_mbi2   = OV["mbi_count"][sdr_id]
        j_conf2  = OV["conf_count"][sdr_id]
        j_held2  = OV["held_count"][sdr_id]
        j_pipe2  = sum(e["amount"] for e in OV["pipeline"][sdr_id])

        sdr_rows = [
            ("Total Dials",                        str(j_dials2)),
            ("Pickups",                            str(j_pick2)),
            ("Conversations",                      str(j_conv2)),
            ("Pickup-to-Conversation Ratio",       _pct(j_conv2,j_pick2)),
            ("Email Volume",                       str(j_email2)),
            ("Social Prospecting Reach Out",       str(j_soc2)),
            ("Completions",                        str(j_comp2)),
            ("Completion Ratio",                   _pct(j_comp2,j_dials2)),
            ("Positive Completions",               str(j_pos2)),
            ("Positive Completions Ratio",         _pct(j_pos2,j_comp2)),
            ("Meetings Booked (Outbound)",         str(j_mbo2)),
            ("Meetings Booked (Self-Prospecting)", str(j_mbsp2)),
            ("Meetings Booked (Inbound)",          str(j_mbi2)),
            ("Meetings Confirmed (Inbound)",       str(j_conf2)),
            ("Meetings Held",                      str(j_held2)),
            ("New Qualified Pipeline",             f"${j_pipe2:,.0f}"),
        ]
        st.dataframe(
            pd.DataFrame(sdr_rows, columns=["Metric","Value"]),
            use_container_width=True, hide_index=True
        )

        # ── LEAD DETAILS (expandable) ─────────────────────────
        with st.expander(f"🔍 {sdr_name} — Lead Details"):

            st.markdown("**Completions**")
            comp_table(R["completions"][sdr_id])

            st.markdown("**Meetings Booked (Outbound)**")
            mbo_table(R["mbo"][sdr_id])

            st.markdown("**Meetings Booked (Self-Prospecting)**")
            leads_table(R["self_prosp"][sdr_id])

            st.markdown("**Meetings Booked (Inbound)**")
            leads_table(R["inbound"][sdr_id])

            st.markdown("**Meetings Confirmed (Inbound)**")
            leads_table(R["confirmed"][sdr_id])

            st.markdown("**Meetings Held**")
            leads_table(R["held"][sdr_id])

            st.markdown("**Pipeline Deals**")
            pipeline_table(R["pipeline"][sdr_id])

    # ── OVERRIDE COUNTS ────────────────────────────────────────
    st.header("3  — Override Any Numbers (optional)")
    st.caption("Changes apply instantly — both the rollup and individual sections update as you type.")

    field_map = [
        ("completions_count", "Completions"),
        ("pos_count",         "Positive Completions"),
        ("mbo_count",         "Mtgs Booked (Outbound)"),
        ("mbsp_count",        "Mtgs Booked (Self-Prosp)"),
        ("mbi_count",         "Mtgs Booked (Inbound)"),
        ("conf_count",        "Mtgs Confirmed"),
        ("held_count",        "Meetings Held"),
    ]
    ov_cols = st.columns(4)
    for i, (key, label) in enumerate(field_map):
        with ov_cols[i % 4]:
            st.markdown(f"**{label}**")
            st.number_input(f"{label} – Jawwad", min_value=0,
                            value=OV[key][JAWWAD], key=f"ov_{key}_j")
            st.number_input(f"{label} – Mohsin", min_value=0,
                            value=OV[key][MOHSIN], key=f"ov_{key}_m")

    # ── FINALIZE → SHEET ──────────────────────────────────────
    st.header("4  — Finalize & Write to Sheet")
    tab_name = week["tab"]

    svc_check = build_sheets_service()
    existing  = tab_exists(svc_check, tab_name)

    if existing:
        st.warning(f"Tab **'{tab_name}'** already exists. Confirm overwrite:")
        overwrite = st.checkbox(f"Yes, delete and recreate '{tab_name}'")
    else:
        overwrite = True

    if st.button("✅ Finalize & Write to Sheet", type="primary",
                 disabled=(existing is not None and not overwrite)):
        with st.spinner("Writing to Google Sheets…"):
            if existing:
                delete_tab(svc_check, existing["properties"]["sheetId"])

            merged_svc = build_sheets_service()
            create_tab(merged_svc, tab_name)

            numeric_sid = write_kpi_sheet(tab_name, OV, tab_name,
                                           week["monday"], week["friday"])

        sheet_url = (f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
                     f"/edit#gid={numeric_sid}")
        st.success(f"Sheet written! [Open {tab_name} →]({sheet_url})")
        st.balloons()
