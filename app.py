"""
청라 식물공장 통합 모니터링 v4
Dash 앱 | 5탭 구성

탭 구성:
  1. 실시간 환경 현황  - FastAPI 온습도 실시간 도면 + 클릭 시 오늘 그래프
  2. 재배 현황        - 재배대별 정식일/성장 단계 도면
  3. 생산 이력        - Google Sheets 수확_이력 전체 컬럼 테이블 + 차트
  4. AI Agent        - Claude tool_use (DB조회/생산이력/재배현황/비교그래프)
  5. 온습도 통계      - 시간대별/계절별 평균 온습도 분포

실행: python app.py
접속: http://127.0.0.1:8050
Vercel: server = app.server
"""

import json
import os
import re
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import plotly.colors as pc
import plotly.graph_objects as go
import requests
from dash import Dash, Input, Output, State, callback, ctx, dash_table, dcc, html
from dash.exceptions import PreventUpdate

# ─────────────────────────────────────────────────────────────────────────────
# 0. 설정
# ─────────────────────────────────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))

FASTAPI_BASE            = "http://168.107.55.96:8000"
FASTAPI_SENSOR_ENDPOINT = "/api/sensors/latest"   # 실시간 최신
FASTAPI_HISTORY_ENDPOINT= "/api/sensors/history"  # ?serial=xxx&hours=N

GSHEET_SPREADSHEET_ID = "19iY6VNhe4T2RVOsIX4vS5vIqHnaw3eWLGts27n17vNE"
GSHEET_HARVEST_SHEET  = "수확_이력"

ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
REFRESH_INTERVAL_MS = 30_000

# KST (UTC+9)
KST = timezone(timedelta(hours=9))

# 서버 시리얼 맵 캐시 (bed_id → serial_number)
_SERIAL_MAP: dict = {}

# ─────────────────────────────────────────────────────────────────────────────
# 1. 정적 데이터 로딩 (통계 탭용 엑셀)
# ─────────────────────────────────────────────────────────────────────────────
df_hourly   = pd.read_excel(os.path.join(HERE, "재배대간_시간대별_온습도_분포.xlsx"))
df_seasonal = pd.read_excel(os.path.join(HERE, "재배대간_계절별_시간대_온습도_분포.xlsx"))
df_summary  = pd.read_excel(
    os.path.join(HERE, "청라_재배대별_온습도.xlsx"),
    sheet_name="재배대별 평균 온습도 데이터",
)
for _df in [df_hourly, df_seasonal, df_summary]:
    _df["재배대"] = _df["재배대"].astype(str)
SEASONS = sorted(df_seasonal["계절"].unique().tolist())

# ─────────────────────────────────────────────────────────────────────────────
# 2. 재배대 현황 JSON
# ─────────────────────────────────────────────────────────────────────────────
BED_STATUS_PATH = os.path.join(HERE, "bed_status.json")
BED_STATUS: dict = {}
if os.path.exists(BED_STATUS_PATH):
    with open(BED_STATUS_PATH, encoding="utf-8") as _f:
        BED_STATUS = json.load(_f)

# ─────────────────────────────────────────────────────────────────────────────
# 3. 재배대 레이아웃 (PDF 도면 기반)
# ─────────────────────────────────────────────────────────────────────────────
BED_LAYOUT = {
    "7" : (15, 93, 18, 7), "6" : (15, 83, 18, 7), "5" : (15, 73, 18, 7),
    "4" : (15, 63, 18, 7), "3" : (15, 53, 18, 7), "2" : (15, 43, 18, 7),
    "1" : (15, 33, 18, 7), "T3": (15, 21, 18, 7), "T2": (15, 12, 18, 7),
    "T1": (15,  3, 18, 7),
    "18": (62, 93, 18, 7), "17": (62, 84, 18, 7), "16": (62, 75, 18, 7),
    "15": (62, 66, 18, 7), "14": (62, 57, 18, 7), "13": (62, 48, 18, 7),
    "12": (62, 39, 18, 7), "11": (62, 30, 18, 7), "10": (62, 21, 18, 7),
    "9" : (62, 12, 18, 7), "8" : (62,  3, 18, 7),
    "19": (85, 79, 9, 16), "20": (85, 34, 9, 16),
}

INIT_HOUR   = 12
INIT_SEASON = "전체"

GROWTH_STAGES = [
    (  7, "#e3f2fd", "#1565c0", "0~7일 (정식 초기)"),
    ( 14, "#f1f8e9", "#33691e", "8~14일"),
    ( 21, "#dff2a8", "#5a7a00", "15~21일 (팁번 초기)"),
    ( 28, "#fff0a0", "#b07800", "22~28일 (팁번 경고)"),
    (999, "#ffd180", "#8c3a00", "29일+ (수확 권장)"),
]

# ─────────────────────────────────────────────────────────────────────────────
# 4. 시간 유틸리티
# ─────────────────────────────────────────────────────────────────────────────

def now_kst() -> datetime:
    return datetime.now(KST)

def utc_str_to_kst(utc_str: str, fmt: str = "%m/%d %H:%M") -> str:
    """'2026-04-30T04:30:47' (UTC) → '04/30 13:30 KST' """
    if not utc_str:
        return ""
    try:
        dt = datetime.fromisoformat(utc_str).replace(tzinfo=timezone.utc)
        return dt.astimezone(KST).strftime(fmt)
    except Exception:
        return utc_str

# ─────────────────────────────────────────────────────────────────────────────
# 5. FastAPI 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

def _parse_bed_id(bed_name: str) -> str:
    """'bed16' → '16',  'T1' → 'T1'"""
    return bed_name[3:] if bed_name.startswith("bed") else bed_name


def fetch_realtime_data():
    """
    GET /api/sensors/latest
    반환: (temp_dict, hum_dict, extra_dict, error)
    부작용: _SERIAL_MAP 갱신
    """
    global _SERIAL_MAP
    try:
        resp = requests.get(f"{FASTAPI_BASE}{FASTAPI_SENSOR_ENDPOINT}", timeout=5)
        resp.raise_for_status()
        items = resp.json().get("data", [])

        temp_vals: dict = {}
        hum_vals:  dict = {}
        extra_vals: dict = {}

        for item in items:
            raw = item.get("bed_name", "")
            if not raw:
                continue
            bid    = _parse_bed_id(raw)
            serial = item.get("serial_number", "")
            t      = item.get("temperature", -1.0)
            h      = item.get("humidity",    -1.0)
            ppm    = item.get("ppm",  -1.0)
            ec     = item.get("ec",   -1.0)
            ts_utc = item.get("created_at", "")

            _SERIAL_MAP[bid] = serial

            if t is not None and float(t) != -1.0:
                temp_vals[bid] = float(t)
            if h is not None and float(h) != -1.0:
                hum_vals[bid]  = float(h)

            extra_vals[bid] = {
                "ppm":    ppm  if ppm  != -1.0 else None,
                "ec":     ec   if ec   != -1.0 else None,
                "ts_kst": utc_str_to_kst(ts_utc),   # KST 변환
            }

        return temp_vals, hum_vals, extra_vals, None

    except requests.exceptions.ConnectionError:
        return {}, {}, {}, "서버 연결 실패"
    except requests.exceptions.Timeout:
        return {}, {}, {}, "요청 시간 초과"
    except Exception as e:
        return {}, {}, {}, str(e)


def fetch_bed_history(bed_id: str, hours: int = 24) -> pd.DataFrame:
    """
    GET /api/sensors/history?serial=xxx&hours=N
    created_at를 KST로 변환하여 반환.
    """
    serial = _SERIAL_MAP.get(bed_id)
    if not serial:
        return pd.DataFrame()
    try:
        resp = requests.get(
            f"{FASTAPI_BASE}{FASTAPI_HISTORY_ENDPOINT}",
            params={"serial": serial, "hours": hours},
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("data", [])
        if not items:
            return pd.DataFrame()
        df = pd.DataFrame(items)
        # created_at: UTC → KST
        df["created_at"] = (
            pd.to_datetime(df["created_at"], utc=True)
            .dt.tz_convert("Asia/Seoul")
            .dt.tz_localize(None)
        )
        for col in ["temperature", "humidity", "ppm", "ec"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
                df.loc[df[col] == -1.0, col] = np.nan
        return df.sort_values("created_at")
    except Exception:
        return pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
# 6. 온습도 통계 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

def get_values(mode: str, hour, season=None) -> dict:
    col = "temp_mean" if mode == "temp" else "hum_mean"
    h   = int(hour) if hour is not None else INIT_HOUR
    if season and season != "전체":
        df = df_seasonal[(df_seasonal["계절"] == season) & (df_seasonal["시간"] == h)]
    else:
        df = df_hourly[df_hourly["시간"] == h]
    return dict(zip(df["재배대"].astype(str), df[col]))


def val_to_color(v, vmin, vmax, colorscale):
    t   = max(0.0, min(1.0, (v - vmin) / (vmax - vmin)))
    rgb = pc.sample_colorscale(colorscale, [t])[0]
    r, g, b = pc.unlabel_rgb(rgb)
    return f"rgb({int(r)},{int(g)},{int(b)})"


def days_to_stage_colors(plant_days: int):
    for max_day, fill, tc, _ in GROWTH_STAGES:
        if plant_days <= max_day:
            return fill, tc
    return GROWTH_STAGES[-1][1], GROWTH_STAGES[-1][2]


# ─────────────────────────────────────────────────────────────────────────────
# 7. Figure 생성
# ─────────────────────────────────────────────────────────────────────────────

def _add_zone_shapes(fig):
    for x0, x1, y0, y1, color in [
        ( 5, 25, -2, 99, "rgba(220,235,255,0.45)"),
        (52, 73, -2, 99, "rgba(220,255,235,0.45)"),
        (80, 91, -2, 99, "rgba(255,240,220,0.45)"),
    ]:
        fig.add_shape(type="rect", x0=x0, y0=y0, x1=x1, y1=y1,
                      fillcolor=color, line=dict(color="#ccc", width=1), layer="below")
    for text, x in [("Beds 1~7, T1~T3", 15), ("Beds 8~18", 62), ("19/20", 85)]:
        fig.add_annotation(x=x, y=-5, text=text, showarrow=False,
                           font=dict(size=9, color="#888"), align="center")


def make_floor_figure(values: dict, mode: str, hour, season_label: str,
                      title_override: str = "", extra: dict = None) -> go.Figure:
    hour       = int(hour) if hour is not None else INIT_HOUR
    col_label  = "온도 (°C)"  if mode == "temp" else "습도 (%)"
    colorscale = "RdYlGn_r"  if mode == "temp" else "RdYlBu"
    vmin, vmax = (18.0, 21.5) if mode == "temp" else (74.0, 84.0)
    if values:
        vmin = min(vmin, min(values.values()))
        vmax = max(vmax, max(values.values()))

    fig = go.Figure()
    _add_zone_shapes(fig)

    fig.add_trace(go.Scatter(
        x=[None], y=[None], mode="markers",
        marker=dict(
            colorscale=colorscale, cmin=vmin, cmax=vmax, color=[vmin],
            colorbar=dict(title=dict(text=col_label, side="right"),
                          thickness=16, len=0.75, tickfont=dict(size=11), y=0.5),
            showscale=True, size=0.1,
        ),
        hoverinfo="skip", showlegend=False,
    ))

    hover_x, hover_y, hover_text, hover_ids = [], [], [], []
    for bed_id, (cx, cy, w, h) in BED_LAYOUT.items():
        val  = values.get(bed_id)
        x0, x1_ = cx - w / 2, cx + w / 2
        y0, y1  = cy - h / 2, cy + h / 2

        fill = val_to_color(val, vmin, vmax, colorscale) if val is not None else "#d0d0d0"
        tc   = "#1a1a2e" if val is not None else "#888"

        fig.add_shape(type="rect", x0=x0, y0=y0, x1=x1_, y1=y1,
                      fillcolor=fill, line=dict(color="white", width=2))
        fig.add_annotation(x=cx, y=cy + h * 0.15, text=f"<b>{bed_id}</b>",
                           showarrow=False, font=dict(size=11, color=tc))
        fig.add_annotation(x=cx, y=cy - h * 0.20,
                           text=f"{val:.1f}" if val is not None else "N/A",
                           showarrow=False, font=dict(size=9, color=tc))

        if val is not None:
            unit  = "°C" if mode == "temp" else "%"
            ex    = (extra or {}).get(bed_id, {})
            ts_kst = ex.get("ts_kst", "")
            ppm   = ex.get("ppm")
            ec    = ex.get("ec")
            hover_parts = [
                f"<b>재배대 {bed_id}</b>",
                f"{'온도' if mode=='temp' else '습도'}: {val:.2f}{unit}",
            ]
            if ts_kst:
                hover_parts.append(f"측정: {ts_kst} KST")
            if ppm is not None:
                hover_parts.append(f"PPM: {ppm:.0f}")
            if ec is not None:
                hover_parts.append(f"EC: {ec:.2f}")
            hover_parts.append("<i>클릭하면 오늘 그래프 표시</i>")

            hover_x.append(cx); hover_y.append(cy)
            hover_ids.append(bed_id)
            hover_text.append("<br>".join(hover_parts))

    fig.add_trace(go.Scatter(
        x=hover_x, y=hover_y, mode="markers",
        marker=dict(size=36, opacity=0, color="rgba(0,0,0,0)"),
        text=hover_text, customdata=hover_ids,
        hovertemplate="%{text}<extra></extra>", showlegend=False,
    ))

    icon  = "🌡" if mode == "temp" else "💧"
    title = title_override or (
        f"{icon} {'온도' if mode=='temp' else '습도'} 분포도 — {season_label}  {hour:02d}:00"
    )
    fig.update_layout(
        title=dict(text=title, font=dict(size=16, family="Malgun Gothic, sans-serif"),
                   x=0.5, xanchor="center", y=0.98),
        xaxis=dict(range=[0, 97], showgrid=False, zeroline=False,
                   showticklabels=False, fixedrange=True),
        yaxis=dict(range=[-8, 99], showgrid=False, zeroline=False,
                   showticklabels=False, fixedrange=True, scaleanchor="x"),
        plot_bgcolor="#f4f6f8", paper_bgcolor="#ffffff",
        margin=dict(l=10, r=90, t=45, b=10),
        height=640, clickmode="event",
    )
    return fig


def make_cultivation_figure() -> go.Figure:
    today = date.today()
    fig   = go.Figure()
    _add_zone_shapes(fig)
    hover_x, hover_y, hover_text, hover_ids = [], [], [], []

    for bed_id, (cx, cy, w, h) in BED_LAYOUT.items():
        x0, x1_ = cx - w / 2, cx + w / 2
        y0, y1  = cy - h / 2, cy + h / 2

        if bed_id.startswith("T"):
            fig.add_shape(type="rect", x0=x0, y0=y0, x1=x1_, y1=y1,
                          fillcolor="#e0e0e0", line=dict(color="white", width=1.5))
            fig.add_annotation(x=cx, y=cy, text=f"<b>{bed_id}</b>",
                               showarrow=False, font=dict(size=10, color="#888"))
            continue

        info = BED_STATUS.get(str(int(bed_id)), BED_STATUS.get(bed_id))
        fill, tc, sub = GROWTH_STAGES[-1][1], GROWTH_STAGES[-1][2], "정보없음"

        if info:
            pds = info.get("plant_date")
            if pds:
                try:
                    plant_days = (today - date.fromisoformat(pds)).days
                    fill, tc   = days_to_stage_colors(plant_days)
                    sub        = f"정식 {plant_days}일차"
                except Exception:
                    sub = pds

        fig.add_shape(type="rect", x0=x0, y0=y0, x1=x1_, y1=y1,
                      fillcolor=fill, line=dict(color="white", width=1.5))
        fig.add_annotation(x=cx, y=cy + 1.3, text=f"<b>{bed_id}</b>",
                           showarrow=False, font=dict(size=11, color=tc))
        fig.add_annotation(x=cx, y=cy - 1.5, text=sub,
                           showarrow=False, font=dict(size=8, color=tc))

        if info:
            pred = info.get("prediction")
            hover_body = (
                f"<b>재배대 {bed_id}번</b><br>"
                f"파종일: {info.get('seed_date','-')}<br>"
                f"정식일: {info.get('plant_date','-')}<br>"
            )
            if pred:
                bh = pred["varieties"].get("버터헤드", {})
                kp = pred["varieties"].get("카이피라", {})
                hover_body += (
                    f"파종후: {pred['seed_days']}일 / 정식후: {pred['plant_days']}일<br>"
                    f"<b>버터헤드</b> {bh.get('current_weight_g',0):.0f}g → 목표({bh.get('days_remaining','?')}일후)<br>"
                    f"<b>카이피라</b> {kp.get('current_weight_g',0):.0f}g → 목표({kp.get('days_remaining','?')}일후)"
                )
        else:
            hover_body = f"<b>재배대 {bed_id}번</b><br>데이터 없음"

        hover_x.append(cx); hover_y.append(cy)
        hover_text.append(hover_body)
        hover_ids.append(bed_id)

    fig.add_trace(go.Scatter(
        x=hover_x, y=hover_y, mode="markers",
        marker=dict(size=36, opacity=0),
        text=hover_text, customdata=hover_ids,
        hovertemplate="%{text}<extra></extra>", showlegend=False,
    ))

    updated = BED_STATUS.get("1", {}).get("updated_at", "미확인")
    fig.update_layout(
        title=dict(
            text=f"🌱 재배 현황 도면 — 기준일: {today}  (최종갱신: {updated})",
            font=dict(size=16, family="Malgun Gothic, sans-serif"),
            x=0.5, xanchor="center",
        ),
        xaxis=dict(range=[0, 97], showgrid=False, zeroline=False,
                   showticklabels=False, fixedrange=True),
        yaxis=dict(range=[-8, 99], showgrid=False, zeroline=False,
                   showticklabels=False, fixedrange=True, scaleanchor="x"),
        plot_bgcolor="#f8f9fa", paper_bgcolor="#fff",
        margin=dict(l=10, r=20, t=55, b=10),
        height=720, clickmode="event",
    )
    return fig


def make_time_series(bed_id: str, mode: str, season) -> go.Figure:
    col    = "temp_mean" if mode == "temp" else "hum_mean"
    col_sd = "temp_sd"   if mode == "temp" else "hum_sd"
    unit   = "°C"        if mode == "temp" else "%"
    label  = "온도"       if mode == "temp" else "습도"

    if season and season != "전체":
        df = df_seasonal[(df_seasonal["재배대"] == bed_id) & (df_seasonal["계절"] == season)].sort_values("시간")
    else:
        df = df_hourly[df_hourly["재배대"] == bed_id].sort_values("시간")

    if df.empty:
        return go.Figure()

    fig = go.Figure()
    if col_sd in df.columns:
        sd = df[col_sd]
        fig.add_trace(go.Scatter(
            x=df["시간"].tolist() + df["시간"].tolist()[::-1],
            y=(df[col]+sd).tolist() + (df[col]-sd).tolist()[::-1],
            fill="toself", fillcolor="rgba(99,179,237,0.2)",
            line=dict(color="rgba(0,0,0,0)"), hoverinfo="skip", showlegend=False,
        ))
    fig.add_trace(go.Scatter(
        x=df["시간"], y=df[col], mode="lines+markers",
        line=dict(color="#2B6CB0", width=2.5),
        marker=dict(size=7, color="#1A365D", line=dict(color="white", width=1.5)),
        name=f"재배대 {bed_id}",
        hovertemplate=f"%{{x:02d}}:00<br>{label}: %{{y:.2f}}{unit}<extra></extra>",
    ))
    fig.update_layout(
        title=dict(text=f"재배대 {bed_id} 시간대별 {label}",
                   font=dict(size=14, family="Malgun Gothic"), x=0.5, xanchor="center"),
        xaxis=dict(title="시간", tickvals=list(range(0,24,3)),
                   ticktext=[f"{h:02d}h" for h in range(0,24,3)], showgrid=True, gridcolor="#eee"),
        yaxis=dict(title=f"{label} ({unit})", showgrid=True, gridcolor="#eee"),
        plot_bgcolor="#fafbfc", paper_bgcolor="#ffffff",
        height=300, margin=dict(l=55, r=20, t=45, b=45), hovermode="x unified",
    )
    return fig


def make_rt_day_graph(bed_id: str) -> go.Figure:
    """실시간 탭 클릭 시: 해당 재배대의 오늘(24h) 온습도 그래프"""
    df = fetch_bed_history(bed_id, hours=24)
    fig = go.Figure()
    if df.empty:
        fig.update_layout(
            title=f"재배대 {bed_id}번 — 오늘 데이터 없음 (시리얼 미확인)",
            height=300, plot_bgcolor="#fafbfc", paper_bgcolor="#fff",
        )
        return fig

    colors = {"temperature": "#e53e3e", "humidity": "#3182CE"}
    labels = {"temperature": "온도(°C)", "humidity": "습도(%)"}

    for metric, color in colors.items():
        if metric in df.columns and df[metric].notna().any():
            fig.add_trace(go.Scatter(
                x=df["created_at"], y=df[metric],
                mode="lines+markers", name=labels[metric],
                line=dict(color=color, width=2),
                marker=dict(size=4),
                hovertemplate=f"%{{x|%H:%M}}<br>{labels[metric]}: %{{y:.2f}}<extra></extra>",
                yaxis="y" if metric == "temperature" else "y2",
            ))

    fig.update_layout(
        title=dict(
            text=f"재배대 {bed_id}번 — 오늘 온습도 추이 (KST)",
            font=dict(size=14, family="Malgun Gothic"), x=0.5, xanchor="center",
        ),
        xaxis=dict(title="시간 (KST)", tickformat="%H:%M", showgrid=True, gridcolor="#eee"),
        yaxis=dict(title="온도 (°C)", showgrid=True, gridcolor="#eee", color="#e53e3e"),
        yaxis2=dict(title="습도 (%)", overlaying="y", side="right", color="#3182CE", showgrid=False),
        plot_bgcolor="#fafbfc", paper_bgcolor="#fff",
        height=320, margin=dict(l=55, r=55, t=50, b=45),
        hovermode="x unified",
        legend=dict(orientation="h", y=1.1),
    )
    return fig


def make_comparison_chart(bed_ids: list, hours: int, metric: str) -> go.Figure:
    """AI Agent 비교 그래프: 여러 재배대를 하나의 그래프에"""
    unit  = "°C" if metric == "temperature" else "%"
    label = "온도" if metric == "temperature" else "습도"
    colors = ["#3182CE","#e53e3e","#38a169","#d69e2e","#9b2c2c","#2c7a7b"]
    fig   = go.Figure()

    for i, bid in enumerate(bed_ids):
        df = fetch_bed_history(bid, hours=hours)
        if df.empty or metric not in df.columns:
            continue
        sub = df[["created_at", metric]].dropna()
        fig.add_trace(go.Scatter(
            x=sub["created_at"], y=sub[metric],
            mode="lines", name=f"재배대 {bid}번",
            line=dict(color=colors[i % len(colors)], width=2),
            hovertemplate=f"재배대 {bid}<br>%{{x|%m/%d %H:%M}}<br>{label}: %{{y:.2f}}{unit}<extra></extra>",
        ))

    days = hours // 24
    fig.update_layout(
        title=dict(
            text=f"재배대 {', '.join(bed_ids)}번 {label} 비교 — 최근 {days}일",
            font=dict(size=15, family="Malgun Gothic"), x=0.5, xanchor="center",
        ),
        xaxis=dict(title="날짜/시간 (KST)", tickformat="%m/%d", showgrid=True, gridcolor="#eee"),
        yaxis=dict(title=f"{label} ({unit})", showgrid=True, gridcolor="#eee"),
        plot_bgcolor="#fafbfc", paper_bgcolor="#fff",
        height=380, margin=dict(l=55, r=20, t=55, b=50),
        hovermode="x unified",
        legend=dict(orientation="h", y=1.08),
    )
    return fig


# ─────────────────────────────────────────────────────────────────────────────
# 8. UI 컴포넌트 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

def make_stats_card(values: dict, mode: str):
    unit = "°C" if mode == "temp" else "%"
    if not values:
        return html.P("데이터 없음", style={"color": "#a0aec0"})
    avg  = np.mean(list(values.values()))
    maxb = max(values, key=values.get)
    minb = min(values, key=values.get)
    return html.Div([
        html.H3("📊 현재 통계",
                style={"fontSize": "13px", "margin": "0 0 10px", "fontWeight": "600"}),
        *[html.Div([
            html.Span(lbl, style={"color": "#718096", "fontSize": "12px"}),
            html.Span(f"{v:.1f}{unit}",
                      style={"fontWeight": "700", "color": c, "fontSize": "13px"}),
        ], style={"display": "flex", "justifyContent": "space-between",
                  "padding": "5px 0", "borderBottom": "1px solid rgba(0,0,0,0.06)"})
          for lbl, v, c in [
              ("전체 평균",           avg,          "#2d3748"),
              (f"최고 (재배대 {maxb})", values[maxb], "#e53e3e"),
              (f"최저 (재배대 {minb})", values[minb], "#3182CE"),
          ]],
        html.P(f"측정 재배대: {len(values)}개",
               style={"fontSize": "11px", "color": "#a0aec0",
                      "marginTop": "8px", "marginBottom": 0}),
    ])


def make_legend():
    return html.Div(
        [html.Span("■ 색상 범례:  ",
                   style={"fontWeight": "700", "fontSize": "12px",
                          "color": "#4a5568", "marginRight": "6px"})]
        + [html.Span(f"■ {label}", style={
            "fontSize": "12px", "color": tc, "background": bg,
            "padding": "3px 8px", "borderRadius": "4px",
            "marginRight": "6px", "fontWeight": "600",
            "border": f"1px solid {tc}33",
        }) for _, bg, tc, label in GROWTH_STAGES],
        style={"display": "flex", "flexWrap": "wrap", "alignItems": "center",
               "padding": "8px 24px", "background": "#fff",
               "borderBottom": "1px solid #e2e8f0", "gap": "4px"},
    )


def _info_row(label, value):
    return html.Div([
        html.Span(label, style={"color": "#718096", "fontSize": "12px"}),
        html.Span(str(value), style={"fontWeight": "600", "color": "#2d3748", "fontSize": "12px"}),
    ], style={"display": "flex", "justifyContent": "space-between",
              "padding": "4px 0", "borderBottom": "1px solid rgba(0,0,0,0.06)"})


def make_bed_detail_card(bed_id_str: str):
    info = BED_STATUS.get(str(int(bed_id_str)), BED_STATUS.get(bed_id_str))
    if not info:
        return html.Div([
            html.H3("🌿 재배대 상세 정보",
                    style={"fontSize": "13px", "margin": "0 0 8px", "fontWeight": "600"}),
            html.P(f"재배대 {bed_id_str}번 데이터 없음",
                   style={"fontSize": "12px", "color": "#a0aec0",
                          "textAlign": "center", "marginTop": "20px"}),
        ])
    pred  = info.get("prediction")
    rows  = [
        html.H3(f"🌿 재배대 {bed_id_str}번 상세",
                style={"fontSize": "14px", "margin": "0 0 12px", "fontWeight": "700"}),
        _info_row("📅 파종일", info.get("seed_date", "-")),
        _info_row("🌱 정식일", info.get("plant_date", "-")),
    ]
    if pred:
        rows += [
            _info_row("🗓 파종후", f"{pred['seed_days']}일"),
            _info_row("📆 정식후", f"{pred['plant_days']}일"),
            html.Hr(style={"margin": "10px 0", "borderColor": "#e2e8f0"}),
            html.P("📊 수확 예측 (목표 130g)",
                   style={"fontWeight": "600", "fontSize": "12px", "margin": "6px 0"}),
        ]
        for variety in ["버터헤드", "카이피라"]:
            v  = pred["varieties"].get(variety, {})
            cw = v.get("current_weight_g", 0)
            dr = v.get("days_remaining")
            td = v.get("target_date")
            color = "#38a169" if cw >= 130 else ("#dd6b20" if cw >= 100 else "#3182ce")
            rows.append(html.Div([
                html.Div(variety, style={"fontWeight": "600", "fontSize": "12px", "color": "#4a5568"}),
                html.Div([
                    html.Span(f"현재 {cw:.0f}g",
                              style={"color": color, "fontWeight": "700", "fontSize": "13px"}),
                    html.Span(
                        f"  {'✅ 수확가능' if dr==0 else f'→ {dr}일 후 ({td})'}"
                        if dr is not None else "",
                        style={"color": "#718096", "fontSize": "11px"},
                    ),
                ]),
            ], style={"padding": "5px 0", "borderBottom": "1px solid rgba(0,0,0,0.06)"}))
    return html.Div(rows)


def _make_summary_card():
    today = date.today()
    harvest_soon, harvest_ready = [], []
    for bid, info in BED_STATUS.items():
        pred = info.get("prediction")
        if not pred:
            continue
        for variety in ["버터헤드", "카이피라"]:
            v  = pred["varieties"].get(variety, {})
            dr = v.get("days_remaining")
            if dr is None:
                continue
            if dr == 0:
                harvest_ready.append(f"{bid}번({variety[:2]})")
            elif dr <= 5:
                harvest_soon.append(f"{bid}번({variety[:2]}, {dr}일후)")
    return [
        html.H3("📊 수확 현황 요약",
                style={"fontSize": "13px", "margin": "0 0 10px", "fontWeight": "600"}),
        html.Div([
            html.Span("✅ 수확 가능", style={"color": "#718096", "fontSize": "12px"}),
            html.Span(", ".join(harvest_ready) if harvest_ready else "없음",
                      style={"fontWeight": "700", "color": "#38a169", "fontSize": "11px"}),
        ], style={"display": "flex", "justifyContent": "space-between",
                  "padding": "5px 0", "borderBottom": "1px solid rgba(0,0,0,0.06)"}),
        html.Div([
            html.Span("⏰ 5일내 수확", style={"color": "#718096", "fontSize": "12px"}),
            html.Span(", ".join(harvest_soon) if harvest_soon else "없음",
                      style={"fontWeight": "700", "color": "#dd6b20", "fontSize": "11px"}),
        ], style={"display": "flex", "justifyContent": "space-between",
                  "padding": "5px 0", "borderBottom": "1px solid rgba(0,0,0,0.06)"}),
        html.P(f"분석 기준: {today} | 목표중량: 130g",
               style={"fontSize": "10px", "color": "#a0aec0",
                      "marginTop": "8px", "marginBottom": 0}),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# 9. Google Sheets 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

def load_harvest_data():
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        key_env = os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY", "")
        if key_env.strip().startswith("{"):
            import tempfile
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, encoding="utf-8"
            )
            tmp.write(key_env); tmp.close()
            creds_path = tmp.name
        elif key_env and os.path.exists(key_env):
            creds_path = key_env
        else:
            creds_path = os.path.join(HERE, "service-account-key.json")

        if not os.path.exists(creds_path):
            return None, "service-account-key.json 없음"

        scopes = ["https://spreadsheets.google.com/feeds",
                  "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
        gc    = gspread.authorize(creds)
        ws    = gc.open_by_key(GSHEET_SPREADSHEET_ID).worksheet(GSHEET_HARVEST_SHEET)
        rows  = ws.get_all_values()
        if len(rows) < 2:
            return None, "시트에 데이터 없음"
        df = pd.DataFrame(rows[1:], columns=rows[0])
        df = df.loc[df.apply(lambda r: r.str.strip().ne("").any(), axis=1)]
        return df.reset_index(drop=True), None
    except ImportError:
        return None, "gspread 패키지 미설치"
    except Exception as e:
        return None, str(e)


# ─────────────────────────────────────────────────────────────────────────────
# 10. AI Agent 도구 정의 + 실행
# ─────────────────────────────────────────────────────────────────────────────

AI_TOOLS = [
    {
        "name": "query_sensor_history",
        "description": (
            "재배대의 온도/습도/PPM/EC 센서 데이터를 조회합니다. "
            "예: '이번주 1번 재배대 평균 온도', '10일 동안 11번 재배대 온도 통계'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bed_id":  {"type": "string", "description": "재배대 번호 ('1'~'20', 'T1'~'T3')"},
                "hours":   {"type": "integer", "description": "조회 기간 (시간): 24=오늘, 168=일주일, 240=10일"},
                "metric":  {"type": "string",
                            "enum": ["temperature","humidity","ppm","ec"],
                            "description": "조회 지표 (기본: temperature)"},
            },
            "required": ["bed_id", "hours"],
        },
    },
    {
        "name": "query_production_history",
        "description": (
            "Google Sheets 수확_이력 시트에서 생산 이력을 조회합니다. "
            "예: '3월 13일 생산이력 현황', '버터헤드 수확량 통계'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date":    {"type": "string",
                            "description": "날짜 또는 월 (예: '2026-03-13' 또는 '2026-03')"},
                "bed_id":  {"type": "string", "description": "재배대 번호 (선택)"},
                "variety": {"type": "string", "description": "품종 (버터헤드/카이피라, 선택)"},
            },
            "required": [],
        },
    },
    {
        "name": "get_cultivation_status",
        "description": (
            "현재 재배 현황 (정식일, 성장 단계, 수확 예측)을 조회합니다. "
            "예: '재배 현황 리포트', '수확 가능한 재배대 알려줘'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bed_id": {"type": "string", "description": "특정 재배대 번호 (선택, 없으면 전체)"},
            },
            "required": [],
        },
    },
    {
        "name": "generate_comparison_chart",
        "description": (
            "여러 재배대의 온도/습도 추이를 하나의 그래프로 비교합니다. "
            "예: '10일 동안 11번 12번 재배대 온도 비교 그래프'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bed_ids": {"type": "array", "items": {"type": "string"},
                            "description": "비교할 재배대 번호 목록 (예: ['11','12'])"},
                "hours":   {"type": "integer", "description": "기간 (시간): 240=10일, 168=7일"},
                "metric":  {"type": "string",
                            "enum": ["temperature","humidity"],
                            "description": "비교 지표"},
            },
            "required": ["bed_ids", "hours", "metric"],
        },
    },
]


def execute_ai_tool(tool_name: str, tool_input: dict):
    """
    AI 도구 실행.
    generate_comparison_chart 의 경우 go.Figure 반환.
    나머지는 str 반환.
    """
    # ── 1. 센서 히스토리 조회 ──────────────────────────────
    if tool_name == "query_sensor_history":
        bid    = str(tool_input.get("bed_id", ""))
        hours  = int(tool_input.get("hours", 24))
        metric = tool_input.get("metric", "temperature")
        unit   = "°C" if metric == "temperature" else ("%" if metric == "humidity" else "")
        label  = {"temperature":"온도","humidity":"습도","ppm":"PPM","ec":"EC"}.get(metric, metric)

        df = fetch_bed_history(bid, hours=hours)
        if df.empty:
            return f"재배대 {bid}번의 데이터를 가져올 수 없습니다. (시리얼 맵 미확인 — 실시간 탭을 먼저 한 번 방문해주세요)"
        if metric not in df.columns or df[metric].dropna().empty:
            return f"재배대 {bid}번에 '{metric}' 데이터가 없습니다."

        vals = df[metric].dropna()
        idx_max = vals.idxmax()
        idx_min = vals.idxmin()
        ts_max  = df.loc[idx_max, "created_at"].strftime("%m/%d %H:%M") if idx_max in df.index else "-"
        ts_min  = df.loc[idx_min, "created_at"].strftime("%m/%d %H:%M") if idx_min in df.index else "-"

        return (
            f"재배대 {bid}번  최근 {hours}시간({hours//24}일)  {label} 통계:\n"
            f"  평균: {vals.mean():.2f}{unit}\n"
            f"  최고: {vals.max():.2f}{unit}  ({ts_max} KST)\n"
            f"  최저: {vals.min():.2f}{unit}  ({ts_min} KST)\n"
            f"  측정 횟수: {len(vals)}회"
        )

    # ── 2. 생산 이력 조회 ──────────────────────────────────
    if tool_name == "query_production_history":
        date_filter    = tool_input.get("date", "")
        bed_filter     = str(tool_input.get("bed_id", ""))
        variety_filter = tool_input.get("variety", "")

        df, err = load_harvest_data()
        if err or df is None:
            return f"수확 데이터 로드 실패: {err}"

        date_col    = "수확 날짜"
        bed_col     = "재배대 넘버"
        variety_col = "품종"
        weight_col  = next((c for c in df.columns if "박스 제외" in c), None)
        count_col   = next((c for c in df.columns if "개체수"   in c), None)
        avg_col     = next((c for c in df.columns if "평균 무게" in c), None)

        if date_col in df.columns:
            df[date_col] = pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d")
        if date_filter and date_col in df.columns:
            key = date_filter.replace(".", "-")
            df  = df[df[date_col].str.startswith(key[:7] if len(key.replace("-","")) <= 6 else key)]
        if bed_filter and bed_col in df.columns:
            df = df[df[bed_col].astype(str) == bed_filter]
        if variety_filter and variety_col in df.columns:
            df = df[df[variety_col].str.contains(variety_filter, na=False)]

        if df.empty:
            return "조건에 맞는 수확 데이터가 없습니다."

        lines = [f"수확 이력 조회 결과 ({len(df)}건):"]
        for _, row in df.head(15).iterrows():
            d_val = row.get(date_col,"")
            b_val = row.get(bed_col,"")
            v_val = row.get(variety_col,"")
            w_val = row.get(weight_col,"") if weight_col else ""
            c_val = row.get(count_col,"")  if count_col else ""
            a_val = row.get(avg_col,"")    if avg_col   else ""
            lines.append(f"  {d_val} | 재배대 {b_val}번 | {v_val} | {w_val}kg | {c_val}개 | 평균 {a_val}g")
        if len(df) > 15:
            lines.append(f"  ... 외 {len(df)-15}건")

        # 합계
        if weight_col:
            try:
                total = pd.to_numeric(df[weight_col], errors="coerce").sum()
                lines.append(f"\n  총 수확량: {total:.2f}kg")
            except Exception:
                pass
        return "\n".join(lines)

    # ── 3. 재배 현황 ──────────────────────────────────────
    if tool_name == "get_cultivation_status":
        bid   = str(tool_input.get("bed_id", ""))
        today = date.today()

        if bid:
            info = BED_STATUS.get(bid, BED_STATUS.get(str(int(bid)) if bid.isdigit() else bid))
            if not info:
                return f"재배대 {bid}번 데이터 없음"
            pred  = info.get("prediction", {})
            lines = [f"재배대 {bid}번 현황 (기준: {today}):"]
            lines.append(f"  파종일: {info.get('seed_date')}")
            lines.append(f"  정식일: {info.get('plant_date')}")
            if pred:
                lines.append(f"  파종후 {pred.get('seed_days')}일  /  정식후 {pred.get('plant_days')}일")
                for variety, vd in pred.get("varieties", {}).items():
                    cw = vd.get("current_weight_g", 0)
                    dr = vd.get("days_remaining")
                    td = vd.get("target_date")
                    lines.append(
                        f"  {variety}: 현재 {cw:.0f}g  "
                        f"{'✅ 수확가능' if dr==0 else f'→ {dr}일 후 ({td})'}"
                    )
            return "\n".join(lines)
        else:
            lines = [f"전체 재배 현황 (기준: {today}):"]
            harvest_ready, harvest_soon = [], []
            for bid_, info in sorted(BED_STATUS.items(),
                                     key=lambda x: int(x[0]) if x[0].isdigit() else 999):
                pred = info.get("prediction", {})
                pd_  = pred.get("plant_days", "?")
                stage = ""
                if isinstance(pd_, int):
                    for max_d, _, _, lbl in GROWTH_STAGES:
                        if pd_ <= max_d:
                            stage = lbl; break
                lines.append(f"  재배대 {bid_:>2}번: 정식 {pd_}일차  [{stage}]")
                for variety, vd in pred.get("varieties", {}).items():
                    dr = vd.get("days_remaining")
                    if dr == 0:
                        harvest_ready.append(f"{bid_}({variety[:2]})")
                    elif dr is not None and dr <= 5:
                        harvest_soon.append(f"{bid_}({variety[:2]}, {dr}일후)")
            if harvest_ready:
                lines.append(f"\n  ✅ 수확 가능: {', '.join(harvest_ready)}")
            if harvest_soon:
                lines.append(f"  ⏰ 5일내 수확: {', '.join(harvest_soon)}")
            return "\n".join(lines)

    # ── 4. 비교 그래프 → Figure 반환 ──────────────────────
    if tool_name == "generate_comparison_chart":
        bed_ids = tool_input.get("bed_ids", [])
        hours   = int(tool_input.get("hours", 240))
        metric  = tool_input.get("metric", "temperature")
        return make_comparison_chart(bed_ids, hours, metric)  # go.Figure

    return f"알 수 없는 도구: {tool_name}"


def _build_system_prompt() -> str:
    today = date.today()
    beds  = []
    for bid, info in list(BED_STATUS.items())[:15]:
        pred = info.get("prediction", {})
        beds.append(
            f"  재배대 {bid}번: 정식일 {info.get('plant_date','?')}, "
            f"정식후 {pred.get('plant_days','?')}일차"
        )
    return (
        f"당신은 청라 식물공장 데이터 분석 AI Agent입니다.\n"
        f"오늘: {today}  |  실시간 서버: {FASTAPI_BASE}\n\n"
        f"현재 재배 현황 (일부):\n" + "\n".join(beds) + "\n\n"
        "사용 가능한 도구:\n"
        "  - query_sensor_history: 재배대 센서 데이터 조회\n"
        "  - query_production_history: 수확 이력 조회\n"
        "  - get_cultivation_status: 재배 현황 조회\n"
        "  - generate_comparison_chart: 재배대 비교 그래프 생성\n\n"
        "한국어로 답변하며, 데이터가 필요한 질문에는 반드시 도구를 사용하세요."
    )


# ─────────────────────────────────────────────────────────────────────────────
# 11. AI 말풍선 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

def _bubble_user(text):
    return html.Div([
        html.Span("👤 "), html.Span(text, style={"whiteSpace": "pre-wrap"}),
    ], style={"background":"#e6fffa","border":"1px solid #81e6d9",
              "borderRadius":"12px","padding":"10px 14px",
              "marginBottom":"8px","textAlign":"right","fontSize":"14px"})

def _bubble_ai(text):
    return html.Div([
        html.Span("🤖 "), html.Span(text, style={"whiteSpace": "pre-wrap"}),
    ], style={"background":"#fff","border":"1px solid #e2e8f0",
              "borderRadius":"12px","padding":"10px 14px",
              "marginBottom":"8px","fontSize":"14px","color":"#2d3748"})

def _bubble_chart(fig):
    return html.Div(
        dcc.Graph(figure=fig, config={"displayModeBar": True},
                  style={"height": "380px"}),
        style={"background":"#fff","border":"1px solid #e2e8f0",
               "borderRadius":"12px","padding":"8px","marginBottom":"8px"}
    )

def _bubble_err(text):
    return html.Div(f"⚠️  {text}",
                    style={"color":"#e53e3e","fontSize":"13px",
                           "background":"#fff5f5","border":"1px solid #feb2b2",
                           "borderRadius":"8px","padding":"10px 14px",
                           "marginBottom":"8px"})


# ─────────────────────────────────────────────────────────────────────────────
# 12. Dash 앱 초기화
# ─────────────────────────────────────────────────────────────────────────────
app    = Dash(__name__, title="청라 식물공장", suppress_callback_exceptions=True)
server = app.server  # Vercel WSGI

SEASON_OPTIONS = [{"label": "📅 전체 평균", "value": "전체"}] + [
    {"label": f"🍂 {s}", "value": s} for s in SEASONS
]
TAB_STYLE = {
    "fontWeight":"700","fontSize":"15px",
    "padding":"12px 28px","textAlign":"center","color":"#4a5568",
}
TAB_SEL_STYLE = {**TAB_STYLE, "borderTop":"3px solid #3182CE","color":"#3182CE"}

app.layout = html.Div([
    # 헤더
    html.Div([
        html.H1("🌱 청라 식물공장 통합 모니터링",
                style={"margin":0,"fontSize":"28px","fontWeight":"800",
                       "color":"#1a365d","textAlign":"center"}),
        html.P("실시간 환경 · 재배 현황 · 생산 이력 · AI Agent · 온습도 통계",
               style={"margin":"6px 0 0","color":"#4a5568",
                      "fontSize":"15px","textAlign":"center","fontWeight":"500"}),
    ], style={"background":"linear-gradient(135deg,#ebf8ff,#e6fffa)",
              "padding":"20px 24px","borderBottom":"2px solid #bee3f8"}),

    # 탭
    dcc.Tabs(id="main-tabs", value="tab-realtime",
        children=[
            dcc.Tab(label="📡 실시간 환경",  value="tab-realtime",
                    style=TAB_STYLE, selected_style=TAB_SEL_STYLE),
            dcc.Tab(label="🌿 재배 현황",    value="tab-cult",
                    style=TAB_STYLE, selected_style=TAB_SEL_STYLE),
            dcc.Tab(label="📦 생산 이력",    value="tab-harvest",
                    style=TAB_STYLE, selected_style=TAB_SEL_STYLE),
            dcc.Tab(label="🤖 AI Agent",    value="tab-ai",
                    style=TAB_STYLE, selected_style=TAB_SEL_STYLE),
            dcc.Tab(label="📊 온습도 통계",  value="tab-stats",
                    style=TAB_STYLE, selected_style=TAB_SEL_STYLE),
        ],
        style={"background":"#fff","borderBottom":"1px solid #e2e8f0","justifyContent":"center"},
    ),

    html.Div(id="tab-content"),

    # 전역 Store
    dcc.Store(id="selected-bed-stats"),
    dcc.Store(id="play-state",   data=False),
    dcc.Store(id="chat-history", data=[]),
    dcc.Store(id="rt-clicked-bed", data=None),

], style={"fontFamily":"'Malgun Gothic',sans-serif",
          "background":"#f7fafc","minHeight":"100vh"})


# ─────────────────────────────────────────────────────────────────────────────
# 13. 탭 렌더링 콜백
# ─────────────────────────────────────────────────────────────────────────────

@callback(Output("tab-content", "children"), Input("main-tabs", "value"))
def render_tab(tab: str):

    # ── 탭 1: 실시간 환경 ─────────────────────────────────
    if tab == "tab-realtime":
        temp_vals, hum_vals, extra, err = fetch_realtime_data()
        now_str  = now_kst().strftime("%Y-%m-%d %H:%M:%S KST")
        err_text = f"⚠️  {err}" if err else f"✅  연결됨 — {now_str}  ({len(temp_vals)}개 센서)"
        err_col  = "#e53e3e" if err else "#38a169"

        init_temp_fig = make_floor_figure(
            temp_vals, "temp", 0, "실시간",
            title_override=f"🌡  실시간 온도 분포 — {now_str}",
            extra=extra,
        )
        init_hum_fig = make_floor_figure(
            hum_vals, "hum", 0, "실시간",
            title_override=f"💧  실시간 습도 분포 — {now_str}",
            extra=extra,
        )

        return html.Div([
            dcc.Interval(id="rt-interval", interval=REFRESH_INTERVAL_MS, n_intervals=0),

            # 상태 바
            html.Div([
                html.Span("FastAPI: ", style={"fontSize":"12px","color":"#718096"}),
                html.Span(f"{FASTAPI_BASE}{FASTAPI_SENSOR_ENDPOINT}",
                          style={"fontSize":"12px","fontWeight":"600","color":"#2d3748"}),
                html.Span("   |   ", style={"color":"#e2e8f0"}),
                html.Span(id="rt-status-text", children=err_text,
                          style={"fontSize":"12px","color":err_col}),
                html.Span("   |   자동갱신 30초",
                          style={"fontSize":"11px","color":"#a0aec0"}),
            ], style={"padding":"8px 24px","background":"#fff",
                      "borderBottom":"1px solid #e2e8f0",
                      "display":"flex","alignItems":"center","flexWrap":"wrap","gap":"4px"}),

            # 도면 2개
            html.Div([
                dcc.Graph(id="rt-temp-graph", figure=init_temp_fig,
                          config={"displayModeBar": False},
                          style={"flex":"1","minWidth":"340px"}),
                dcc.Graph(id="rt-hum-graph", figure=init_hum_fig,
                          config={"displayModeBar": False},
                          style={"flex":"1","minWidth":"340px"}),
            ], style={"display":"flex","flexWrap":"wrap","gap":"14px",
                      "padding":"14px 24px","background":"#f7fafc"}),

            # 클릭 시 오늘 그래프 패널
            html.Div([
                html.Div(id="rt-day-graph-container",
                         children=html.P(
                             "도면에서 재배대를 클릭하면 오늘 온습도 그래프가 표시됩니다.",
                             style={"color":"#a0aec0","fontSize":"13px",
                                    "textAlign":"center","padding":"20px 0"}
                         ),
                         style={"background":"#fff","borderRadius":"8px",
                                "border":"1px solid #e2e8f0",
                                "padding":"12px","flex":"1"}),
            ], style={"padding":"0 24px 24px","display":"flex","gap":"14px"}),
        ])

    # ── 탭 2: 재배 현황 ────────────────────────────────────
    if tab == "tab-cult":
        return html.Div([
            make_legend(),
            html.Div([
                dcc.Graph(id="cult-floor-graph", figure=make_cultivation_figure(),
                          config={"displayModeBar": False}, style={"flex":"1"}),
                html.Div([
                    html.Div(id="cult-detail-card",
                             children=html.Div([
                                 html.H3("🌿 재배대 상세 정보",
                                         style={"fontSize":"13px","margin":"0 0 8px","fontWeight":"600"}),
                                 html.P("도면에서 재배대를 클릭하세요",
                                        style={"fontSize":"12px","color":"#718096",
                                               "textAlign":"center","marginTop":"30px"}),
                             ]),
                             style={"background":"#fff","borderRadius":"8px",
                                    "border":"1px solid #e2e8f0",
                                    "padding":"14px","marginBottom":"12px"}),
                    html.Div(_make_summary_card(),
                             style={"background":"linear-gradient(135deg,#ebf8ff,#e6fffa)",
                                    "borderRadius":"8px","border":"1px solid #bee3f8",
                                    "padding":"14px"}),
                ], style={"width":"300px","flexShrink":0}),
            ], style={"display":"flex","gap":"14px","padding":"14px 24px","background":"#f7fafc"}),
        ])

    # ── 탭 3: 생산 이력 ────────────────────────────────────
    if tab == "tab-harvest":
        df, err = load_harvest_data()
        if err or df is None:
            return html.Div([html.Div([
                html.H3("📦 생산 이력 — Google Sheets",
                        style={"fontSize":"18px","fontWeight":"700","margin":"0 0 16px"}),
                html.Div([
                    html.P(f"⚠️  데이터 로드 실패: {err}",
                           style={"color":"#e53e3e","fontSize":"14px","margin":"0 0 8px"}),
                    html.Ul([
                        html.Li("service-account-key.json 을 앱 폴더에 배치"),
                        html.Li("또는 GOOGLE_SERVICE_ACCOUNT_KEY 환경변수에 JSON 내용 저장"),
                        html.Li(f"시트 ID: {GSHEET_SPREADSHEET_ID}"),
                        html.Li(f"시트명: {GSHEET_HARVEST_SHEET}"),
                    ], style={"fontSize":"12px","color":"#718096","lineHeight":"1.8"}),
                ], style={"background":"#fff5f5","border":"1px solid #feb2b2",
                          "borderRadius":"8px","padding":"16px"}),
            ], style={"padding":"24px","maxWidth":"700px"})])

        cols = df.columns.tolist()

        # 날짜 컬럼 파싱
        DATE_COL = "수확 날짜"
        if DATE_COL in df.columns:
            df[DATE_COL] = pd.to_datetime(df[DATE_COL], errors="coerce").dt.strftime("%Y-%m-%d")

        WEIGHT_COL  = next((c for c in cols if "박스 제외" in c), None) or \
                      next((c for c in cols if "무게" in c), None)
        VARIETY_COL = "품종" if "품종" in cols else None

        # 차트
        chart_fig = go.Figure()
        if DATE_COL in cols and WEIGHT_COL:
            try:
                pf = df.copy()
                pf[WEIGHT_COL] = pd.to_numeric(pf[WEIGHT_COL], errors="coerce")
                pf = pf.dropna(subset=[DATE_COL, WEIGHT_COL])
                pf = pf[pf[WEIGHT_COL] > 0]
                if not pf.empty:
                    c_map = {"버터헤드":"#3182CE","카이피라":"#38a169"}
                    if VARIETY_COL:
                        for var in sorted(pf[VARIETY_COL].unique()):
                            agg = pf[pf[VARIETY_COL]==var].groupby(DATE_COL)[WEIGHT_COL].sum().reset_index()
                            chart_fig.add_trace(go.Bar(
                                x=agg[DATE_COL], y=agg[WEIGHT_COL],
                                name=str(var), marker_color=c_map.get(str(var),"#718096"),
                            ))
                    else:
                        agg = pf.groupby(DATE_COL)[WEIGHT_COL].sum().reset_index()
                        chart_fig.add_trace(go.Bar(
                            x=agg[DATE_COL], y=agg[WEIGHT_COL],
                            name="수확량", marker_color="#3182CE",
                        ))
                    chart_fig.update_layout(
                        title="날짜별 수확량 (kg, 박스 제외)",
                        xaxis_title="수확 날짜", yaxis_title="수확량 (kg)",
                        barmode="stack", plot_bgcolor="#fafbfc", paper_bgcolor="#fff",
                        height=360, margin=dict(l=60,r=20,t=50,b=80),
                        font=dict(family="Malgun Gothic"), xaxis=dict(tickangle=-45),
                        legend=dict(orientation="h", y=1.05),
                    )
            except Exception:
                pass

        return html.Div([
            html.Div([
                html.H3("📦 생산 이력 — Google Sheets",
                        style={"fontSize":"18px","fontWeight":"700","margin":"0 0 4px"}),
                html.P(f"시트: {GSHEET_HARVEST_SHEET}  |  총 {len(df)}건  |  컬럼 {len(cols)}개",
                       style={"fontSize":"13px","color":"#718096","margin":0}),
            ], style={"padding":"16px 24px","background":"#fff",
                      "borderBottom":"1px solid #e2e8f0"}),

            html.Div([dcc.Graph(figure=chart_fig, config={"displayModeBar":False})],
                     style={"padding":"14px 24px"}),

            html.Div([
                dash_table.DataTable(
                    data=df.to_dict("records"),
                    columns=[{"name": c.replace("\n"," "), "id": c} for c in cols],
                    page_size=25,
                    sort_action="native", filter_action="native",
                    style_table={"overflowX":"auto"},
                    style_header={"backgroundColor":"#ebf8ff","fontWeight":"700",
                                  "fontSize":"12px","border":"1px solid #bee3f8",
                                  "whiteSpace":"normal"},
                    style_cell={"fontSize":"12px","padding":"7px 10px",
                                "border":"1px solid #e2e8f0","fontFamily":"Malgun Gothic",
                                "whiteSpace":"normal","height":"auto","minWidth":"60px"},
                    style_data_conditional=[
                        {"if":{"row_index":"odd"},"backgroundColor":"#f7fafc"},
                        {"if":{"filter_query":'{품종} = "버터헤드"'},"color":"#2B6CB0"},
                        {"if":{"filter_query":'{품종} = "카이피라"'},"color":"#276749"},
                    ],
                ),
            ], style={"padding":"0 24px 24px"}),
        ])

    # ── 탭 4: AI Agent ─────────────────────────────────────
    if tab == "tab-ai":
        api_ok    = bool(ANTHROPIC_API_KEY)
        api_badge = (
            html.Span("✅ API 키 설정됨", style={"color":"#38a169","fontSize":"12px","fontWeight":"600"})
            if api_ok else
            html.Span("⚠️ ANTHROPIC_API_KEY 미설정", style={"color":"#e53e3e","fontSize":"12px","fontWeight":"600"})
        )

        return html.Div([
            html.Div([
                html.H3("🤖 AI Agent — Claude",
                        style={"fontSize":"18px","fontWeight":"700","margin":"0 0 4px"}),
                html.P(["데이터 조회 도구 포함 (센서/생산이력/재배현황/비교그래프)   ", api_badge],
                       style={"fontSize":"13px","color":"#718096","margin":0}),
            ], style={"padding":"16px 24px","background":"#fff",
                      "borderBottom":"1px solid #e2e8f0"}),

            html.Div([
                # 채팅창
                html.Div(id="chat-messages",
                         children=[_bubble_ai(
                             "안녕하세요! 청라 식물공장 AI Agent입니다.\n\n"
                             "다음과 같이 질문하세요:\n"
                             "  • 이번주 1번 재배대 평균 온도 알려줘\n"
                             "  • 3월 13일 생산 이력 현황\n"
                             "  • 재배 현황 리포트해줘\n"
                             "  • 10일 동안 11번 12번 재배대 온도 비교 그래프"
                         )],
                         style={"height":"480px","overflowY":"auto",
                                "background":"#f7fafc","borderRadius":"8px",
                                "border":"1px solid #e2e8f0",
                                "padding":"16px","marginBottom":"12px"}),

                # 입력
                dcc.Textarea(id="chat-input", placeholder="메시지 입력...",
                             style={"width":"100%","height":"72px","borderRadius":"8px",
                                    "border":"1px solid #e2e8f0","padding":"10px 14px",
                                    "fontSize":"14px","fontFamily":"Malgun Gothic",
                                    "resize":"none","boxSizing":"border-box","outline":"none"}),
                html.Div([
                    html.Button("전송 ▶", id="chat-send-btn", n_clicks=0,
                                style={"background":"#3182CE","color":"white","border":"none",
                                       "borderRadius":"6px","padding":"10px 28px",
                                       "cursor":"pointer","fontSize":"14px","fontWeight":"700"}),
                    html.Button("초기화", id="chat-clear-btn", n_clicks=0,
                                style={"background":"#e2e8f0","color":"#4a5568","border":"none",
                                       "borderRadius":"6px","padding":"10px 18px",
                                       "cursor":"pointer","fontSize":"13px"}),
                ], style={"display":"flex","gap":"8px","marginTop":"8px","justifyContent":"flex-end"}),
            ], style={"padding":"16px 24px","maxWidth":"960px","margin":"0 auto"}),
        ])

    # ── 탭 5: 온습도 통계 ──────────────────────────────────
    if tab == "tab-stats":
        init_vals = get_values("temp", INIT_HOUR, INIT_SEASON)
        init_fig  = make_floor_figure(init_vals, "temp", INIT_HOUR, "전체 평균")
        init_card = make_stats_card(init_vals, "temp")

        return html.Div([
            dcc.Store(id="stats-mode-store", data="temp"),

            html.Div([
                html.Div([
                    html.Label("📊 지표", style={"fontWeight":"600","fontSize":"12px","color":"#4a5568"}),
                    dcc.RadioItems(id="stats-mode-radio",
                                  options=[{"label":" 🌡 온도","value":"temp"},
                                           {"label":" 💧 습도","value":"hum"}],
                                  value="temp", inline=True,
                                  inputStyle={"marginRight":"4px"},
                                  labelStyle={"marginRight":"16px","fontSize":"13px","fontWeight":"600"}),
                ], style={"flex":"0 0 160px"}),
                html.Div([
                    html.Label("🍂 계절", style={"fontWeight":"600","fontSize":"12px","color":"#4a5568"}),
                    dcc.Dropdown(id="stats-season-dd", options=SEASON_OPTIONS,
                                 value=INIT_SEASON, clearable=False,
                                 style={"width":"170px","fontSize":"13px"}),
                ], style={"flex":"0 0 185px"}),
                html.Div([
                    html.Label(id="stats-hour-label", children=f"🕐 시간: {INIT_HOUR:02d}:00",
                               style={"fontWeight":"600","fontSize":"12px","color":"#4a5568"}),
                    dcc.Slider(id="stats-hour-slider", min=0, max=23, step=1, value=INIT_HOUR,
                               marks={h: f"{h:02d}" for h in range(0,24,3)},
                               tooltip={"placement":"bottom","always_visible":False}),
                ], style={"flex":"1","minWidth":"280px"}),
                html.Div([
                    html.Button("▶ 재생", id="stats-play-btn", n_clicks=0,
                                style={"background":"#3182CE","color":"white","border":"none",
                                       "borderRadius":"6px","padding":"8px 16px",
                                       "cursor":"pointer","fontSize":"13px","fontWeight":"600"}),
                    dcc.Interval(id="stats-anim-interval", interval=800, n_intervals=0, disabled=True),
                ], style={"display":"flex","alignItems":"flex-end"}),
            ], style={"display":"flex","flexWrap":"wrap","gap":"20px","alignItems":"flex-end",
                      "padding":"14px 24px","background":"#fff",
                      "boxShadow":"0 1px 3px rgba(0,0,0,0.1)"}),

            html.Div([
                dcc.Graph(id="stats-floor-graph", figure=init_fig,
                          config={"displayModeBar":False}, style={"flex":"1"}),
                html.Div([
                    html.Div([
                        html.H3("📈 시간대별 추이",
                                style={"fontSize":"13px","margin":"0 0 8px","fontWeight":"600"}),
                        html.P("도면에서 재배대를 클릭하세요", id="stats-ts-hint",
                               style={"fontSize":"12px","color":"#718096",
                                      "textAlign":"center","marginTop":"30px"}),
                        dcc.Graph(id="stats-ts-graph", config={"displayModeBar":False},
                                  style={"display":"none","height":"280px"}),
                    ], style={"background":"#fff","borderRadius":"8px",
                              "border":"1px solid #e2e8f0",
                              "padding":"14px","marginBottom":"12px"}),
                    html.Div(id="stats-card", children=init_card,
                             style={"background":"linear-gradient(135deg,#ebf8ff,#e6fffa)",
                                    "borderRadius":"8px","border":"1px solid #bee3f8",
                                    "padding":"14px"}),
                ], style={"width":"300px","flexShrink":0}),
            ], style={"display":"flex","gap":"14px","padding":"14px 24px","background":"#f7fafc"}),
        ])


# ─────────────────────────────────────────────────────────────────────────────
# 14. 콜백 — 탭 1: 실시간 갱신
# ─────────────────────────────────────────────────────────────────────────────

@callback(
    Output("rt-temp-graph",  "figure"),
    Output("rt-hum-graph",   "figure"),
    Output("rt-status-text", "children"),
    Output("rt-status-text", "style"),
    Input("rt-interval",     "n_intervals"),
    prevent_initial_call=True,
)
def update_realtime(_n):
    temp_vals, hum_vals, extra, err = fetch_realtime_data()
    now_str = now_kst().strftime("%Y-%m-%d %H:%M:%S KST")

    temp_fig = make_floor_figure(
        temp_vals, "temp", 0, "실시간",
        title_override=f"🌡  실시간 온도 분포 — {now_str}",
        extra=extra,
    )
    hum_fig = make_floor_figure(
        hum_vals, "hum", 0, "실시간",
        title_override=f"💧  실시간 습도 분포 — {now_str}",
        extra=extra,
    )
    if err:
        return temp_fig, hum_fig, f"⚠️  {err}", {"fontSize":"12px","color":"#e53e3e"}
    return (temp_fig, hum_fig,
            f"✅  연결됨 — {now_str}  ({len(temp_vals)}개 센서)",
            {"fontSize":"12px","color":"#38a169"})


@callback(
    Output("rt-clicked-bed", "data"),
    Input("rt-temp-graph",   "clickData"),
    Input("rt-hum-graph",    "clickData"),
    prevent_initial_call=True,
)
def rt_store_click(cd_t, cd_h):
    cd = cd_t or cd_h
    if cd and "points" in cd:
        cdata = cd["points"][0].get("customdata")
        if cdata and not str(cdata).startswith("T"):
            return str(cdata)
    return None


@callback(
    Output("rt-day-graph-container", "children"),
    Input("rt-clicked-bed",          "data"),
    prevent_initial_call=True,
)
def rt_show_day_graph(bed_id):
    if not bed_id:
        return html.P("도면에서 재배대를 클릭하면 오늘 온습도 그래프가 표시됩니다.",
                      style={"color":"#a0aec0","fontSize":"13px",
                             "textAlign":"center","padding":"20px 0"})
    fig = make_rt_day_graph(bed_id)
    return html.Div([
        html.P(f"재배대 {bed_id}번 — 오늘(24h) 기록  (KST)",
               style={"fontWeight":"600","fontSize":"13px","margin":"0 0 4px","color":"#2d3748"}),
        dcc.Graph(figure=fig, config={"displayModeBar":False}),
    ])


# ─────────────────────────────────────────────────────────────────────────────
# 15. 콜백 — 탭 2: 재배 현황 클릭
# ─────────────────────────────────────────────────────────────────────────────

@callback(
    Output("cult-detail-card", "children"),
    Input("cult-floor-graph",  "clickData"),
    prevent_initial_call=True,
)
def cult_click(cd):
    default = html.Div([
        html.H3("🌿 재배대 상세 정보",
                style={"fontSize":"13px","margin":"0 0 8px","fontWeight":"600"}),
        html.P("도면에서 재배대를 클릭하세요",
               style={"fontSize":"12px","color":"#718096",
                      "textAlign":"center","marginTop":"30px"}),
    ])
    if not cd or "points" not in cd:
        return default
    cdata = cd["points"][0].get("customdata")
    if not cdata or str(cdata).startswith("T"):
        return default
    return make_bed_detail_card(str(cdata))


# ─────────────────────────────────────────────────────────────────────────────
# 16. 콜백 — 탭 4: AI Agent (tool_use)
# ─────────────────────────────────────────────────────────────────────────────

@callback(
    Output("chat-messages", "children"),
    Output("chat-history",  "data"),
    Output("chat-input",    "value"),
    Input("chat-send-btn",  "n_clicks"),
    Input("chat-clear-btn", "n_clicks"),
    State("chat-input",     "value"),
    State("chat-history",   "data"),
    State("chat-messages",  "children"),
    prevent_initial_call=True,
)
def handle_chat(send_n, clear_n, user_input, history, current_msgs):
    triggered = ctx.triggered_id

    if triggered == "chat-clear-btn":
        return [_bubble_ai(
            "안녕하세요! 청라 식물공장 AI Agent입니다.\n\n"
            "다음과 같이 질문하세요:\n"
            "  • 이번주 1번 재배대 평균 온도 알려줘\n"
            "  • 3월 13일 생산 이력 현황\n"
            "  • 재배 현황 리포트해줘\n"
            "  • 10일 동안 11번 12번 재배대 온도 비교 그래프"
        )], [], ""

    if triggered == "chat-send-btn" and user_input and user_input.strip():
        msgs    = list(current_msgs) if current_msgs else []
        history = list(history) if history else []
        text    = user_input.strip()

        msgs.append(_bubble_user(text))

        if not ANTHROPIC_API_KEY:
            msgs.append(_bubble_err("ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다."))
            return msgs, history, ""

        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            history.append({"role": "user", "content": text})

            # ── 1차 호출 (tool_use 포함) ──────────────────
            resp = client.messages.create(
                model="claude-opus-4-6",
                max_tokens=2048,
                system=_build_system_prompt(),
                tools=AI_TOOLS,
                messages=history[-14:],
            )

            tool_results = []
            chart_figs   = []

            # ── 도구 실행 ─────────────────────────────────
            for block in resp.content:
                if block.type == "tool_use":
                    result = execute_ai_tool(block.name, block.input)

                    if isinstance(result, go.Figure):
                        # 비교 그래프
                        chart_figs.append(result)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": "그래프를 생성했습니다.",
                        })
                    else:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(result),
                        })

            # ── 2차 호출 (도구 결과 포함) ─────────────────
            if tool_results:
                history.append({"role": "assistant",
                                 "content": [b.model_dump() for b in resp.content]})
                history.append({"role": "user", "content": tool_results})

                resp2 = client.messages.create(
                    model="claude-opus-4-6",
                    max_tokens=2048,
                    system=_build_system_prompt(),
                    tools=AI_TOOLS,
                    messages=history[-16:],
                )
                reply = next(
                    (b.text for b in resp2.content if hasattr(b, "text")), ""
                )
                history.append({"role": "assistant", "content": reply})
            else:
                reply = next(
                    (b.text for b in resp.content if hasattr(b, "text")), ""
                )
                history.append({"role": "assistant", "content": reply})

            # ── 결과 메시지 추가 ──────────────────────────
            for fig in chart_figs:
                msgs.append(_bubble_chart(fig))
            if reply:
                msgs.append(_bubble_ai(reply))

        except Exception as e:
            msgs.append(_bubble_err(str(e)))

        return msgs, history, ""

    raise PreventUpdate


# ─────────────────────────────────────────────────────────────────────────────
# 17. 콜백 — 탭 5: 온습도 통계
# ─────────────────────────────────────────────────────────────────────────────

@callback(Output("stats-hour-label","children"), Input("stats-hour-slider","value"))
def upd_stats_label(h):
    return f"🕐 시간: {int(h):02d}:00"

@callback(Output("stats-mode-store","data"), Input("stats-mode-radio","value"))
def upd_stats_mode(mode):
    return mode

@callback(
    Output("stats-floor-graph","figure"),
    Output("stats-card","children"),
    Input("stats-mode-store","data"),
    Input("stats-hour-slider","value"),
    Input("stats-season-dd","value"),
)
def upd_stats_floor(mode, hour, season):
    if mode is None or hour is None:
        return go.Figure(), html.P("로딩 중...", style={"color":"#a0aec0"})
    vals = get_values(mode, int(hour), season)
    sl   = season if (season and season != "전체") else "전체 평균"
    return make_floor_figure(vals, mode, int(hour), sl), make_stats_card(vals, mode)

@callback(
    Output("selected-bed-stats","data"),
    Input("stats-floor-graph","clickData"),
    prevent_initial_call=True,
)
def store_stats_click(cd):
    if cd and "points" in cd:
        m = re.search(r"재배대 (\w+)", cd["points"][0].get("text",""))
        if m:
            return m.group(1)
    return None

@callback(
    Output("stats-ts-graph","figure"),
    Output("stats-ts-graph","style"),
    Output("stats-ts-hint","style"),
    Input("selected-bed-stats","data"),
    Input("stats-mode-store","data"),
    Input("stats-season-dd","value"),
)
def upd_stats_ts(bed_id, mode, season):
    hidden    = {"display":"none"}
    show      = {"height":"280px"}
    hint_show = {"fontSize":"12px","color":"#718096","textAlign":"center","marginTop":"30px"}
    if not bed_id or mode is None:
        return go.Figure(), hidden, hint_show
    return make_time_series(bed_id, mode, season or "전체"), show, {"display":"none"}

@callback(
    Output("stats-hour-slider","value"),
    Output("stats-anim-interval","disabled"),
    Output("stats-play-btn","children"),
    Output("play-state","data"),
    Input("stats-play-btn","n_clicks"),
    Input("stats-anim-interval","n_intervals"),
    Input("play-state","data"),
    Input("stats-hour-slider","value"),
    prevent_initial_call=True,
)
def animate_stats(_nc, _ni, playing, hour):
    tid = ctx.triggered_id
    if tid == "stats-play-btn":
        new = not playing
        return hour, not new, ("⏸ 일시정지" if new else "▶ 재생"), new
    if tid == "stats-anim-interval" and playing:
        return (int(hour)+1)%24, False, "⏸ 일시정지", True
    return hour, not playing, ("⏸ 일시정지" if playing else "▶ 재생"), playing


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n" + "="*52)
    print("  청라 식물공장 통합 모니터링 v4")
    print("  http://127.0.0.1:8050")
    print("="*52 + "\n")
    app.run(debug=True, port=8050)
