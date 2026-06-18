"""
全国都市計画GISビューア ＋ 敷地法規調査 統合ツール
（このファイルが Streamlit Cloud の起動エントリーポイント）

タブ構成:
  Tab 1 - 🗺️ GISビューア : reinfolib 用途地域ポリゴン・防火・地区計画・道路マップ
  Tab 2 - 📋 法規レポート : 国土数値情報 A29 + Web検索 による詳細法規チェック
  Tab 3 - 🏗️ ボリューム検討 : 建ぺい率・容積率から建物エンベロープを 3D 可視化
"""

import math
import os
import re
import sys
from pathlib import Path

import folium
import plotly.graph_objects as go
import requests
import streamlit as st
from shapely.geometry import Point, shape
from streamlit_folium import st_folium

# analyze_site.py（同フォルダ）から法規調査関数をインポート
sys.path.insert(0, str(Path(__file__).parent))
from analyze_site import build_report, geocode, research, volume_study

# ────────────────────────────────────────────────
# 定数
# ────────────────────────────────────────────────

REVERSE_GEO_URL = "https://mreversegeocoder.gsi.go.jp/reverse-geocoder/LonLatToAddress"
REINFOLIB_BASE  = "https://www.reinfolib.mlit.go.jp/ex-api/external"
GSI_TILE        = "https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png"
GSI_ATTR        = '<a href="https://maps.gsi.go.jp/development/ichiran.html" target="_blank">地理院タイル (国土地理院)</a>'

ZONE_COLORS: dict[str, str] = {
    "第一種低層住居専用地域":   "#8DC353",
    "第二種低層住居専用地域":   "#C3D93E",
    "田園住居地域":             "#D7D739",
    "第一種中高層住居専用地域": "#73B2E0",
    "第二種中高層住居専用地域": "#5585C8",
    "第一種住居地域":           "#FFF100",
    "第二種住居地域":           "#F7C900",
    "準住居地域":               "#F5A622",
    "近隣商業地域":             "#F5842C",
    "商業地域":                 "#E83838",
    "準工業地域":               "#C890CC",
    "工業地域":                 "#AB88CA",
    "工業専用地域":             "#8070BA",
}

# reinfolib の use_area_ja は表記ゆれ（全角アラビア数字・空白）があるため正規化して照合する
_ZEN2HAN_DIGIT = str.maketrans("０１２３４５６７８９", "0123456789")


def _normalize_zone(name: str) -> str:
    if not name:
        return ""
    s = name.translate(_ZEN2HAN_DIGIT)
    s = s.replace("　", "").replace(" ", "").strip()
    # 用途地域名の数字は「一」「二」のみ。アラビア数字で来ても漢数字に寄せる
    s = s.replace("1", "一").replace("2", "二")
    return s


# 正規化キーで引けるようにした色辞書
ZONE_COLORS_NORM: dict[str, str] = {_normalize_zone(k): v for k, v in ZONE_COLORS.items()}


def zone_color(name: str) -> str:
    return ZONE_COLORS_NORM.get(_normalize_zone(name), "#AAAAAA")


_DEFAULT_ZOOM = 15


# ────────────────────────────────────────────────
# バックエンド: GIS / reinfolib
# ────────────────────────────────────────────────

def _lat_lon_to_tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    n = 2 ** zoom
    x = int((lon + 180) / 360 * n)
    lat_rad = math.radians(lat)
    y = int((1 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2 * n)
    return x, y


def _reinfolib_features(endpoint: str, lat: float, lon: float, api_key: str, zoom: int = _DEFAULT_ZOOM) -> list[dict]:
    x, y = _lat_lon_to_tile(lat, lon, zoom)
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    params  = {"response_format": "geojson", "z": zoom, "x": x, "y": y}
    resp = requests.get(f"{REINFOLIB_BASE}/{endpoint}", headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get("features", [])


def _reinfolib_features_robust(endpoint: str, lat: float, lon: float, api_key: str) -> list[dict]:
    """zoom15 でフィーチャが空なら zoom14 も試みる。"""
    for zoom in [15, 14]:
        try:
            feats = _reinfolib_features(endpoint, lat, lon, api_key, zoom)
            if feats:
                return feats
        except Exception:
            pass
    return []


def _hit(features: list[dict], lat: float, lon: float) -> dict | None:
    """完全一致を優先し、なければ 20m 以内の最近傍フィーチャを返す。"""
    pt = Point(lon, lat)
    for feat in features:
        try:
            if shape(feat["geometry"]).covers(pt):
                return feat
        except Exception:
            pass
    nearest, min_dist = None, float("inf")
    for feat in features:
        try:
            d = shape(feat["geometry"]).distance(pt)
            if d < min_dist:
                min_dist, nearest = d, feat
        except Exception:
            pass
    return nearest if (nearest is not None and min_dist < 0.0002) else None


def _dynamic_attr(props: dict) -> str | None:
    return next((v for v in props.values() if isinstance(v, str) and len(v) > 2), None)


def reverse_geocode(lat: float, lon: float) -> str:
    try:
        resp = requests.get(REVERSE_GEO_URL, params={"lat": lat, "lon": lon}, timeout=8)
        place = resp.json().get("results", {}).get("lv01Nm", "")
        return f"{place}付近（クリック）" if place else f"地図クリック ({lat:.4f}, {lon:.4f})"
    except Exception:
        return f"地図クリック ({lat:.4f}, {lon:.4f})"


def fetch_planning_info(lat: float, lon: float, api_key: str) -> dict:
    result: dict = {
        "zone": None, "bcr": None, "far": None,
        "fire": None, "kubun": None, "chiku": None, "koudo": None,
        "tochiseibi": None, "douro": None,
        "errors": [],
        "zone_features": [], "fire_features": [], "chiku_features": [], "douro_features": [],
    }

    try:
        feats = _reinfolib_features_robust("XKT002", lat, lon, api_key)
        result["zone_features"] = feats
        hit = _hit(feats, lat, lon)
        if hit:
            p = hit["properties"]
            result["zone"] = p.get("use_area_ja")
            result["bcr"]  = p.get("u_building_coverage_ratio_ja")
            result["far"]  = p.get("u_floor_area_ratio_ja")
    except Exception as e:
        result["errors"].append(f"XKT002: {e}")

    try:
        feats = _reinfolib_features_robust("XKT001", lat, lon, api_key)
        hit = _hit(feats, lat, lon)
        if hit:
            result["kubun"] = hit["properties"].get("area_classification_ja")
    except Exception as e:
        result["errors"].append(f"XKT001: {e}")

    try:
        feats = _reinfolib_features_robust("XKT014", lat, lon, api_key)
        result["fire_features"] = feats
        hit = _hit(feats, lat, lon)
        if hit:
            result["fire"] = hit["properties"].get("fire_prevention_ja")
    except Exception as e:
        result["errors"].append(f"XKT014: {e}")

    try:
        feats = _reinfolib_features_robust("XKT023", lat, lon, api_key)
        result["chiku_features"] = feats
        hit = _hit(feats, lat, lon)
        if hit:
            p = hit["properties"]
            result["chiku"] = (
                p.get("district_plan_name_ja") or p.get("name_ja")
                or p.get("name") or _dynamic_attr(p)
            )
    except Exception as e:
        result["errors"].append(f"XKT023: {e}")

    try:
        feats = _reinfolib_features_robust("XKT024", lat, lon, api_key)
        hit = _hit(feats, lat, lon)
        if hit:
            p = hit["properties"]
            result["koudo"] = (
                p.get("high_use_district_ja") or p.get("name_ja")
                or p.get("name") or _dynamic_attr(p)
            )
    except Exception as e:
        result["errors"].append(f"XKT024: {e}")

    try:
        feats = _reinfolib_features_robust("XKT003", lat, lon, api_key)
        hit = _hit(feats, lat, lon)
        if hit:
            p = hit["properties"]
            result["tochiseibi"] = (
                p.get("location_normalization_plan_area_ja") or p.get("area_type_ja")
                or p.get("zone_type_ja") or _dynamic_attr(p)
            )
    except Exception as e:
        result["errors"].append(f"XKT003: {e}")

    try:
        feats = _reinfolib_features_robust("XKT030", lat, lon, api_key)
        result["douro_features"] = feats
        hit = _hit(feats, lat, lon)
        if hit:
            p = hit["properties"]
            result["douro"] = (
                p.get("road_name_ja") or p.get("city_planning_road_name_ja")
                or p.get("name_ja") or p.get("name")
                or _dynamic_attr(p) or "計画区域内"
            )
    except Exception as e:
        result["errors"].append(f"XKT030: {e}")

    if result["zone"] and not result["kubun"]:
        result["kubun"] = "市街化区域"

    return result


# ────────────────────────────────────────────────
# 3D ボリューム可視化
# ────────────────────────────────────────────────

def _create_volume_3d(vol: dict, site_w: float, site_d: float) -> go.Figure:
    max_building_area = vol["max_building_area"]
    est_height  = vol["est_height"]
    est_floors  = vol["est_floors"]

    ratio  = site_w / site_d if site_d > 0 else 1.0
    bldg_w = math.sqrt(max_building_area * ratio)
    bldg_d = math.sqrt(max_building_area / ratio)
    pad_x  = max((site_w - bldg_w) / 2, 0)
    pad_y  = max((site_d - bldg_d) / 2, 0)

    x0, x1 = pad_x, pad_x + bldg_w
    y0, y1 = pad_y, pad_y + bldg_d
    h = est_height

    vx = [x0, x1, x1, x0, x0, x1, x1, x0]
    vy = [y0, y0, y1, y1, y0, y0, y1, y1]
    vz = [0,  0,  0,  0,  h,  h,  h,  h ]

    fi = [0, 0, 4, 4, 0, 0, 2, 3, 1, 1, 0, 0]
    fj = [1, 2, 5, 6, 5, 4, 6, 6, 5, 6, 3, 7]
    fk = [2, 3, 6, 7, 1, 5, 3, 7, 6, 2, 7, 4]

    edges = [(0,1),(1,2),(2,3),(3,0),(4,5),(5,6),(6,7),(7,4),(0,4),(1,5),(2,6),(3,7)]
    wx, wy, wz = [], [], []
    for p1, p2 in edges:
        wx += [vx[p1], vx[p2], None]
        wy += [vy[p1], vy[p2], None]
        wz += [vz[p1], vz[p2], None]

    dim_off = max(site_w * 0.12, 1.0)
    h_off   = max(site_w * 0.10, 0.8)

    traces = [
        go.Mesh3d(
            x=[0, site_w, site_w, 0], y=[0, 0, site_d, site_d], z=[0, 0, 0, 0],
            color="#C8B99A", opacity=0.55, name="敷地", showlegend=True, hoverinfo="none",
        ),
        go.Scatter3d(
            x=[0, site_w, site_w, 0, 0], y=[0, 0, site_d, site_d, 0], z=[0.05]*5,
            mode="lines", line=dict(color="#8B7355", width=4), name="敷地境界",
        ),
        go.Mesh3d(
            x=vx, y=vy, z=vz, i=fi, j=fj, k=fk,
            color="#5B8C5A", opacity=0.30, name="建物エンベロープ", showlegend=True, hoverinfo="none",
        ),
        go.Scatter3d(
            x=wx, y=wy, z=wz,
            mode="lines", line=dict(color="#2C3E35", width=2),
            name="建物輪郭", showlegend=False, hoverinfo="none",
        ),
        go.Scatter3d(
            x=[0, site_w], y=[-dim_off, -dim_off], z=[0, 0],
            mode="lines", line=dict(color="#8B7355", width=2, dash="dot"),
            showlegend=False, hoverinfo="none",
        ),
        go.Scatter3d(
            x=[x1 + h_off, x1 + h_off], y=[y0, y0], z=[0, h],
            mode="lines", line=dict(color="#3D5C3C", width=2, dash="dot"),
            showlegend=False, hoverinfo="none",
        ),
        go.Scatter3d(
            x=[x0, x1], y=[y0 - dim_off * 0.5, y0 - dim_off * 0.5], z=[0, 0],
            mode="lines", line=dict(color="#5B8C5A", width=2, dash="dot"),
            showlegend=False, hoverinfo="none",
        ),
        go.Scatter3d(
            x=[site_w / 2,        x1 + h_off * 1.6,  x0 + bldg_w / 2],
            y=[-dim_off * 1.3,    y0,                 y0 - dim_off * 0.6],
            z=[0,                 h / 2,              0],
            mode="text",
            text=[
                f"<b>敷地 {site_w:.1f}×{site_d:.1f} m</b>",
                f"<b>H={h:.1f}m ({est_floors}F)</b>",
                f"建物 {bldg_w:.1f}×{bldg_d:.1f} m",
            ],
            textfont=dict(color="#2C3E35", size=12),
            showlegend=False, hoverinfo="none",
        ),
    ]

    floor_h = h / est_floors if est_floors > 0 else h
    for f in range(1, min(est_floors, 30)):
        fz = f * floor_h
        traces.append(go.Scatter3d(
            x=[x0, x1, x1, x0, x0], y=[y0, y0, y1, y1, y0], z=[fz]*5,
            mode="lines", line=dict(color="#7FAF7E", width=1),
            showlegend=False, hoverinfo="none",
        ))

    fig = go.Figure(data=traces)
    fig.update_layout(
        scene=dict(
            xaxis=dict(showticklabels=False, title="", showgrid=False, zeroline=False),
            yaxis=dict(showticklabels=False, title="", showgrid=False, zeroline=False),
            zaxis=dict(title="高さ (m)", ticksuffix="m", tickfont=dict(color="#2C3E35")),
            aspectmode="data",
            bgcolor="#F8F5EE",
            camera=dict(eye=dict(x=1.6, y=-1.6, z=0.9)),
        ),
        paper_bgcolor="#F5F2EB",
        margin=dict(l=0, r=0, t=10, b=0),
        height=450,
        legend=dict(
            x=0.01, y=0.98,
            bgcolor="rgba(245,242,235,0.85)",
            bordercolor="#D4CFC4",
            borderwidth=1,
            font=dict(color="#2C3E35", size=12),
        ),
    )
    return fig


# ────────────────────────────────────────────────
# セッションステート初期化
# ────────────────────────────────────────────────

for _key, _default in [
    ("lat", None), ("lon", None), ("address", ""),
    ("geo", None), ("info", {}), ("processed_click", None),
    ("zone_info", None), ("report", None),
    ("site_w", 0.0), ("site_d", 0.0), ("road_w", 0.0),
]:
    if _key not in st.session_state:
        st.session_state[_key] = _default

# ────────────────────────────────────────────────
# ページ設定・シークレット（set_page_config は最初の Streamlit 呼び出し）
# ────────────────────────────────────────────────

st.set_page_config(
    page_title="都市計画GIS＋敷地法規調査",
    page_icon="🗺️",
    layout="wide",
)

try:
    for _k in ("REINFOLIB_API_KEY", "GOOGLE_API_KEY"):
        if _k not in os.environ and _k in st.secrets:
            os.environ[_k] = st.secrets[_k]
except Exception:
    pass

api_key = os.environ.get("REINFOLIB_API_KEY", "")

st.markdown("""
<style>
[data-testid="stMetric"] {
    background: #FFFFFF;
    border: 1px solid #C8D8E8;
    border-radius: 12px;
    padding: 0.75rem 0.9rem;
    box-shadow: 0 2px 8px rgba(26,75,140,0.07);
    overflow: hidden;
}
[data-testid="stMetricLabel"] > div {
    font-size: 0.75rem !important;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
[data-testid="stMetricValue"] > div {
    font-size: 0.9rem !important;
    font-weight: 700 !important;
    line-height: 1.3 !important;
    word-break: break-all;
    overflow-wrap: break-word;
    white-space: normal !important;
}
[data-testid="stForm"] {
    background: #FFFFFF;
    border: 1px solid #C8D8E8;
    border-radius: 14px;
    padding: 1.2rem 1.5rem 1rem;
    box-shadow: 0 2px 10px rgba(26,75,140,0.08);
}
</style>
""", unsafe_allow_html=True)

# ────────────────────────────────────────────────
# ヘッダー
# ────────────────────────────────────────────────

st.title("🗺️ 全国都市計画GISビューア ＋ 敷地法規調査")
st.caption(
    "住所を検索するか**地図上をクリック**して都市計画情報を表示。"
    "「法規レポート」タブで建基法・都市計画法の詳細チェック、"
    "「ボリューム検討」タブで 3D エンベロープ可視化ができます。"
    "　データ: reinfolib API（令和6年度）＋ 国土数値情報 A29（2019）"
)

if not api_key:
    st.warning(
        "**REINFOLIB_API_KEY が未設定です。** "
        "GIS ビューアの用途地域ポリゴン取得には reinfolib API キーが必要です。"
        "　法規レポートは API キーなしでも動作します（A29 + Web 検索を使用）。",
        icon="⚠️",
    )

# ────────────────────────────────────────────────
# 住所検索フォーム
# ────────────────────────────────────────────────

with st.form("search_form"):
    address_input = st.text_input(
        "住所を入力",
        placeholder="例）東京都千代田区丸の内1-1-1　または　〒292-0007 千葉県木更津市中島2627-1",
        help="番地まで入力するほど座標精度が上がります。郵便番号は無視されます。",
    )
    with st.expander("🏗️ ボリューム検討（任意）— 入力するとボリューム検討タブで 3D 表示できます"):
        col_w, col_d, col_r = st.columns(3)
        site_w_in = col_w.number_input(
            "敷地 幅（m）— 間口", min_value=0.0, value=0.0, step=0.5, format="%.1f",
            help="前面道路側の寸法",
        )
        site_d_in = col_d.number_input(
            "敷地 奥行（m）", min_value=0.0, value=0.0, step=0.5, format="%.1f",
        )
        road_w_in = col_r.number_input(
            "前面道路 幅員（m）", min_value=0.0, value=0.0, step=0.5, format="%.1f",
            help="前面道路容積率（建基法第52条第2項）の計算に使用",
        )
    submitted = st.form_submit_button("🔍 検索", use_container_width=True, type="primary")

if submitted and address_input.strip():
    clean = re.sub(r"[〒\s]*\d{3}-\d{4}\s*", "", address_input).strip()
    with st.spinner("検索中…"):
        try:
            geo = geocode(clean)  # analyze_site.geocode() → muniCode キーを返す
            st.session_state.geo     = geo
            st.session_state.lat     = geo["lat"]
            st.session_state.lon     = geo["lon"]
            st.session_state.address = geo["normalized"]
            st.session_state.processed_click = None
            st.session_state.zone_info = None
            st.session_state.report    = None
            st.session_state.site_w    = site_w_in
            st.session_state.site_d    = site_d_in
            st.session_state.road_w    = road_w_in
            if api_key:
                st.session_state.info = fetch_planning_info(geo["lat"], geo["lon"], api_key)
            else:
                st.session_state.info = {}
        except ValueError as e:
            st.error(f"住所が見つかりませんでした: {e}")
        except Exception as e:
            st.error(f"エラーが発生しました: {e}")
elif submitted:
    st.warning("住所を入力してください。")

# ────────────────────────────────────────────────
# タブ
# ────────────────────────────────────────────────

tab1, tab2, tab3 = st.tabs(["🗺️ GISビューア", "📋 法規レポート", "🏗️ ボリューム検討"])

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB 1: GIS MAP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

with tab1:
    _lat  = st.session_state.lat or 35.6762
    _lon  = st.session_state.lon or 139.6503
    _zoom = 16 if st.session_state.lat else 6

    m = folium.Map(location=[_lat, _lon], zoom_start=_zoom, tiles=GSI_TILE, attr=GSI_ATTR)
    _info = st.session_state.get("info", {})

    # 用途地域レイヤー (XKT002)
    zone_feats = _info.get("zone_features", [])
    if zone_feats:
        zone_layer = folium.FeatureGroup(name="用途地域 (XKT002)", show=True)
        for feat in zone_feats:
            p     = feat.get("properties", {})
            zname = p.get("use_area_ja", "")
            bcr   = p.get("u_building_coverage_ratio_ja", "")
            far   = p.get("u_floor_area_ratio_ja", "")
            color = zone_color(zname)
            tip   = f"<b>{zname}</b><br>建ぺい率 {bcr}　容積率 {far}" if zname else "（用途地域なし）"
            try:
                folium.GeoJson(
                    feat,
                    style_function=lambda x, c=color: {
                        "fillColor": c, "color": "#333333", "weight": 0.8, "fillOpacity": 0.50,
                    },
                    tooltip=folium.Tooltip(tip, sticky=False),
                    popup=folium.Popup(tip, max_width=220),
                ).add_to(zone_layer)
            except Exception:
                pass
        zone_layer.add_to(m)

    # 防火地域レイヤー (XKT014)
    fire_feats = _info.get("fire_features", [])
    if fire_feats:
        fire_layer = folium.FeatureGroup(name="防火・準防火地域 (XKT014)", show=True)
        for feat in fire_feats:
            fname  = feat.get("properties", {}).get("fire_prevention_ja", "")
            fcolor = "#FF4444" if "防火地域" in fname and "準" not in fname else "#FF9944"
            try:
                folium.GeoJson(
                    feat,
                    style_function=lambda x, c=fcolor: {
                        "fillColor": c, "color": c, "weight": 1.5, "fillOpacity": 0.20,
                    },
                    tooltip=folium.Tooltip(fname, sticky=False),
                ).add_to(fire_layer)
            except Exception:
                pass
        fire_layer.add_to(m)

    # 地区計画レイヤー (XKT023)
    chiku_feats = _info.get("chiku_features", [])
    if chiku_feats:
        chiku_layer = folium.FeatureGroup(name="地区計画 (XKT023)", show=False)
        for feat in chiku_feats:
            p = feat.get("properties", {})
            cname = p.get("district_plan_name_ja") or p.get("name_ja") or p.get("name", "地区計画")
            try:
                folium.GeoJson(
                    feat,
                    style_function=lambda x: {
                        "fillColor": "#00AAAA", "color": "#007777", "weight": 1.5, "fillOpacity": 0.20,
                    },
                    tooltip=folium.Tooltip(cname, sticky=False),
                ).add_to(chiku_layer)
            except Exception:
                pass
        chiku_layer.add_to(m)

    # 都市計画道路レイヤー (XKT030)
    douro_feats = _info.get("douro_features", [])
    if douro_feats:
        douro_layer = folium.FeatureGroup(name="都市計画道路 (XKT030)", show=True)
        for feat in douro_feats:
            p = feat.get("properties", {})
            dname = (
                p.get("road_name_ja") or p.get("city_planning_road_name_ja")
                or p.get("name_ja") or p.get("name", "都市計画道路")
            )
            try:
                folium.GeoJson(
                    feat,
                    style_function=lambda x: {
                        "fillColor": "#8B4513", "color": "#8B4513", "weight": 2.0, "fillOpacity": 0.35,
                    },
                    tooltip=folium.Tooltip(dname, sticky=False),
                ).add_to(douro_layer)
            except Exception:
                pass
        douro_layer.add_to(m)

    # 敷地マーカー
    if st.session_state.lat:
        folium.Marker(
            [st.session_state.lat, st.session_state.lon],
            popup=folium.Popup(st.session_state.address, max_width=250),
            icon=folium.Icon(color="red", icon="home", prefix="glyphicon"),
            tooltip="調査地点",
        ).add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)

    if not st.session_state.lat:
        st.info("地図上をクリックするか、上の検索フォームで住所を入力してください。", icon="👆")

    map_data = st_folium(m, use_container_width=True, height=560, returned_objects=["last_clicked"])

    # 地図クリック処理
    if map_data and map_data.get("last_clicked"):
        clat = map_data["last_clicked"]["lat"]
        clng = map_data["last_clicked"]["lng"]
        if st.session_state.processed_click != (clat, clng):
            st.session_state.processed_click = (clat, clng)
            st.session_state.lat     = clat
            st.session_state.lon     = clng
            st.session_state.address = reverse_geocode(clat, clng)
            st.session_state.geo     = {
                "lat": clat, "lon": clng,
                "normalized": st.session_state.address,
                "muniCode": "",
            }
            st.session_state.zone_info = None
            st.session_state.report    = None
            if api_key:
                st.session_state.info = fetch_planning_info(clat, clng, api_key)
            st.rerun()

    # 情報パネル
    if st.session_state.lat and st.session_state.info:
        _info = st.session_state.info
        st.divider()
        st.subheader(f"📍 {st.session_state.address}")
        st.caption(f"緯度 {st.session_state.lat:.6f} / 経度 {st.session_state.lon:.6f}")

        na = "⚠️ 要確認"
        c1, c2, c3, c4, c5 = st.columns([3, 1.2, 1.2, 1.8, 1.8])
        c1.metric("用途地域",    _info.get("zone")  or na)
        c2.metric("建ぺい率",    _info.get("bcr")   or na)
        c3.metric("容積率",      _info.get("far")   or na)
        c4.metric("防火規制",    _info.get("fire")  or "指定なし")
        c5.metric("区域区分",    _info.get("kubun") or na)

        extra = [
            ("地区計画",       _info.get("chiku")),
            ("高度利用地区",   _info.get("koudo")),
            ("立地適正化計画", _info.get("tochiseibi")),
            ("都市計画道路",   _info.get("douro")),
        ]
        extra_found = [(lbl, val) for lbl, val in extra if val]
        if extra_found:
            cols = st.columns(len(extra_found))
            for col, (lbl, val) in zip(cols, extra_found):
                col.metric(lbl, val)

        if _info.get("errors"):
            st.caption(f"取得エラー（一部データなし）: {', '.join(_info['errors'])}")

        # reinfolib で未取得の項目がある場合に参照リンクを表示
        if any(v is None for v in [_info.get("zone"), _info.get("bcr"), _info.get("far")]):
            st.info(
                "⚠️ reinfolib で取得できなかった項目があります。"
                "詳細は **[toshikeikaku-info.jp](https://toshikeikaku-info.jp/)** または"
                "「📋 法規レポート」タブの A29 調査でご確認ください。",
                icon="🔗",
            )

        st.divider()

        # 用途地域 凡例
        st.subheader("🎨 用途地域 凡例")
        legend_html = "<div style='display:flex;flex-wrap:wrap;gap:8px;'>"
        for zname, color in ZONE_COLORS.items():
            legend_html += (
                f"<div style='display:flex;align-items:center;gap:5px;padding:4px 8px;"
                f"background:{color}22;border-left:4px solid {color};border-radius:4px;"
                f"font-size:0.82rem;'>"
                f"<span style='width:12px;height:12px;background:{color};"
                f"display:inline-block;border-radius:2px;'></span>{zname}</div>"
            )
        legend_html += "</div>"
        st.markdown(legend_html, unsafe_allow_html=True)

        st.divider()
        st.info(
            "**データについて** — 国土交通省 不動産情報ライブラリ API（令和6年度）から取得。"
            "確認申請・設計判断には必ず所管行政庁の窓口で最新情報を確認してください。",
            icon="ℹ️",
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB 2: 法規レポート
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

with tab2:
    if not st.session_state.lat:
        st.info("先に住所を検索するか、GISビューアタブで地図をクリックしてください。", icon="👆")
    else:
        st.subheader(f"📍 {st.session_state.address}")
        st.caption(f"緯度 {st.session_state.lat:.6f} / 経度 {st.session_state.lon:.6f}")

        st.markdown(
            "> 🔗 reinfolib で取得できなかった項目は "
            "[toshikeikaku-info.jp](https://toshikeikaku-info.jp/) で手動確認できます（住所を入力して検索）。"
        )

        if not st.session_state.zone_info:
            st.info(
                "「法規レポートを生成」を押すと、**国土数値情報 A29**（一次情報・公式）と "
                "Web 検索による詳細な法規チェックレポートを生成します。\n\n"
                "初回は A29 データ（都道府県単位 ZIP）のダウンロードが入るため "
                "**30〜60 秒** 程度かかります。2回目以降はキャッシュが効き高速です。",
                icon="📋",
            )
            if st.button("📋 法規レポートを生成", type="primary", use_container_width=True):
                geo = st.session_state.geo or {
                    "lat": st.session_state.lat,
                    "lon": st.session_state.lon,
                    "normalized": st.session_state.address,
                    "muniCode": "",
                }
                query_addr = re.sub(
                    r"[〒\s]*\d{3}-\d{4}\s*", "", geo.get("normalized", st.session_state.address)
                ).strip()

                with st.spinner(
                    "調査中 — 国土数値情報 A29 ダウンロード・Web 検索を含むため"
                    " 30〜60 秒かかる場合があります…"
                ):
                    try:
                        zone_info, search_results, gemini_raw = research(
                            query_addr, geo["normalized"], geo
                        )
                        report = build_report(
                            query_addr, geo, zone_info, search_results, gemini_raw
                        )
                        st.session_state.zone_info = zone_info
                        st.session_state.report    = report
                        st.rerun()
                    except Exception as e:
                        st.error(f"レポート生成中にエラーが発生しました: {e}")

        if st.session_state.zone_info and st.session_state.report:
            zone_info = st.session_state.zone_info
            na = "⚠️ 要確認"

            c1, c2, c3, c4 = st.columns([3, 1.2, 1.2, 1.8])
            c1.metric("用途地域",     zone_info.get("用途地域", na))
            c2.metric("容積率",       zone_info.get("容積率",   na))
            c3.metric("建ぺい率",     zone_info.get("建蔽率",   na))
            c4.metric("防火規制",     zone_info.get("防火規制", na))

            st.divider()
            st.subheader("📋 詳細レポート")
            st.markdown(st.session_state.report)

            st.divider()
            safe_name = re.sub(r'[\\/:*?"<>|]', "_", st.session_state.address)
            st.download_button(
                label="📥 レポートをダウンロード（Markdown）",
                data=st.session_state.report.encode("utf-8"),
                file_name=f"敷地調査_{safe_name}.md",
                mime="text/markdown",
                use_container_width=True,
            )

            if st.button("🔄 レポートを再生成", help="最新の Web 情報で再取得します"):
                st.session_state.zone_info = None
                st.session_state.report    = None
                st.rerun()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB 3: ボリューム検討
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

with tab3:
    if not st.session_state.lat:
        st.info("先に住所を検索してください。", icon="👆")
    elif not st.session_state.zone_info:
        st.info(
            "「📋 法規レポート」タブで **法規レポートを生成** してから、"
            "このタブでボリューム検討を行ってください。",
            icon="📋",
        )
    elif not (st.session_state.get("site_w", 0) > 0 and st.session_state.get("site_d", 0) > 0):
        st.info(
            "検索フォームの **「🏗️ ボリューム検討」欄** で"
            "敷地の幅・奥行きを入力してから検索してください。",
            icon="📐",
        )
    else:
        site_w    = float(st.session_state.site_w)
        site_d    = float(st.session_state.site_d)
        road_w    = float(st.session_state.road_w) if st.session_state.road_w > 0 else None
        site_area = site_w * site_d

        st.subheader(f"🏗️ ボリューム検討 — {st.session_state.address}")
        st.caption(
            f"敷地 {site_w:.1f} × {site_d:.1f} m　（{site_area:,.1f} ㎡）　"
            + (f"前面道路幅員 {road_w:.1f} m" if road_w else "前面道路幅員 未入力")
        )

        vol = volume_study(st.session_state.zone_info, site_area, road_w)

        if "error" in vol:
            st.warning(vol["error"])
        else:
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("最大建築面積",   f"{vol['max_building_area']:,.1f} ㎡")
            far_label = f"{vol['effective_far_pct']:.0f}%"
            if vol.get("far_limited_by_road"):
                far_label += "（道路制限）"
            c2.metric("有効容積率",     far_label)
            c3.metric("最大延べ床面積", f"{vol['max_total_area']:,.1f} ㎡")
            c4.metric("概算最大階数",   f"{vol['est_floors']} 階")

            if vol.get("far_limited_by_road"):
                st.warning(
                    f"前面道路幅員 {road_w}m による容積率制限（{vol['road_far_pct']:.0f}%）が"
                    f"指定容積率（{vol['far_pct']:.0f}%）より厳しいため、有効容積率は "
                    f"**{vol['effective_far_pct']:.0f}%** になります。"
                    "（建基法第52条第2項）"
                )
            if "abs_height_limit" in vol:
                st.warning(
                    f"この用途地域（{vol['zone_name']}）には絶対高さ制限があります。"
                    f"最大 {vol['abs_height_limit']} — 概算階数は {vol['abs_height_limited_floors']} 程度。"
                    "（建基法第55条）"
                )

            st.divider()
            st.markdown("**📐 建物エンベロープ（3D）**")
            st.caption("※敷地を矩形で近似した概算値。前面道路容積率は建基法第52条第2項による。")
            fig = _create_volume_3d(vol, site_w, site_d)
            st.plotly_chart(fig, use_container_width=True)


# ────────────────────────────────────────────────
# サイドバー
# ────────────────────────────────────────────────

with st.sidebar:
    st.header("📖 使い方")
    st.markdown("""
**① 住所を入力 → 「検索」**

または

**② GISビューアタブで地図をクリック**

→ **GISビューア** に用途地域・防火・地区計画ポリゴンを表示

→ **法規レポート** タブで「レポートを生成」を押すと
国土数値情報 A29（一次情報）+ Web検索 による詳細法規チェックレポートを生成

→ 敷地寸法を入力済みなら **ボリューム検討** タブで 3D エンベロープを確認

---

**GISレイヤー（reinfolib API）**

| レイヤー | API | 色 |
|---------|-----|-----|
| 用途地域 | XKT002 | ゾーン別 |
| 防火・準防火 | XKT014 | 赤・橙 |
| 地区計画 | XKT023 | 青緑 |
| 都市計画道路 | XKT030 | 茶 |

---

**法規レポートの信頼度**

✅ = 国土数値情報 A29（一次情報・公式）

⚠️ = Web 参考（要確認）

✕ = 未取得

---
⚠️ 本ツールは参考情報です。確認申請・設計判断は行政窓口で確認してください。
""")

    st.header("🔑 API キー状態")
    if api_key:
        st.success("✅ REINFOLIB_API_KEY 設定済み")
    else:
        st.error("❌ REINFOLIB_API_KEY 未設定")
        st.markdown("[reinfolib API キー取得](https://www.reinfolib.mlit.go.jp/)（無料）")

    google_key = os.environ.get("GOOGLE_API_KEY", "")
    if google_key:
        st.success("✅ GOOGLE_API_KEY 設定済み（Gemini 利用）")
    else:
        st.caption("GOOGLE_API_KEY 未設定 → DuckDuckGo で代替（法規レポートは動作します）")

    st.divider()
    st.caption(
        "データ: reinfolib API (XKT001-030) 令和6年度 / "
        "国土数値情報 A29 2019 / 地理院タイル"
    )
