"""
全国都市計画GISビューア
バックエンドロジック＋Streamlit UI（このファイルが Streamlit Cloud の起動エントリーポイント）
"""

import math
import os
import re

import folium
import requests
import streamlit as st
from shapely.geometry import Point, shape
from streamlit_folium import st_folium

# ────────────────────────────────────────────────
# 定数
# ────────────────────────────────────────────────

GEOCODER_URL    = "https://msearch.gsi.go.jp/address-search/AddressSearch"
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

_DEFAULT_ZOOM = 15


# ────────────────────────────────────────────────
# バックエンド関数
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
    """フィールド名が未知のフィーチャから最初の意味ある文字列属性を取得。"""
    return next((v for v in props.values() if isinstance(v, str) and len(v) > 2), None)


def geocode(address: str) -> dict:
    resp = requests.get(GEOCODER_URL, params={"q": address}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError(f"住所が見つかりません: {address}")
    item   = data[0]
    coords = item["geometry"]["coordinates"]
    props  = item["properties"]
    lat, lon = float(coords[1]), float(coords[0])
    muni_code = props.get("muniCode", "")
    if not muni_code:
        try:
            rev = requests.get(REVERSE_GEO_URL, params={"lat": lat, "lon": lon}, timeout=10)
            muni_code = rev.json().get("results", {}).get("muniCd", "")
        except Exception:
            pass
    return {"lat": lat, "lon": lon, "normalized": props.get("title", address), "muni_code": muni_code}


def reverse_geocode(lat: float, lon: float) -> str:
    """緯度経度 → 住所文字列（地図クリック時の表示用）。"""
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

    # XKT002: 用途地域
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

    # XKT001: 区域区分
    try:
        feats = _reinfolib_features_robust("XKT001", lat, lon, api_key)
        hit = _hit(feats, lat, lon)
        if hit:
            result["kubun"] = hit["properties"].get("area_classification_ja")
    except Exception as e:
        result["errors"].append(f"XKT001: {e}")

    # XKT014: 防火・準防火地域
    try:
        feats = _reinfolib_features_robust("XKT014", lat, lon, api_key)
        result["fire_features"] = feats
        hit = _hit(feats, lat, lon)
        if hit:
            result["fire"] = hit["properties"].get("fire_prevention_ja")
    except Exception as e:
        result["errors"].append(f"XKT014: {e}")

    # XKT023: 地区計画
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

    # XKT024: 高度利用地区
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

    # XKT003: 立地適正化計画
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

    # XKT030: 都市計画道路
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
# セッションステートの初期化（UI の最初に必須）
# ────────────────────────────────────────────────

for _key, _default in [
    ("lat", None), ("lon", None), ("address", ""), ("info", {}), ("processed_click", None),
]:
    if _key not in st.session_state:
        st.session_state[_key] = _default

# ────────────────────────────────────────────────
# ページ設定・シークレット
# ────────────────────────────────────────────────

st.set_page_config(
    page_title="全国都市計画GISビューア",
    page_icon="🗺️",
    layout="wide",
)

try:
    for _k in ("REINFOLIB_API_KEY",):
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
    padding: 1rem 1.2rem;
    box-shadow: 0 2px 8px rgba(26,75,140,0.07);
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

st.title("🗺️ 全国都市計画GISビューア")
st.caption(
    "住所を検索するか、**地図上をクリック**して都市計画情報を表示します。"
    "　データ: reinfolib API（令和6年度）/ 地理院タイル"
)

if not api_key:
    st.warning("**REINFOLIB_API_KEY が未設定です。** 都市計画情報の取得には reinfolib API キーが必要です。", icon="⚠️")

# ────────────────────────────────────────────────
# 住所検索フォーム
# ────────────────────────────────────────────────

with st.form("search_form"):
    address_input = st.text_input(
        "住所を入力",
        placeholder="例）東京都千代田区丸の内1-1-1　または　大阪市北区梅田1-1",
        help="番地まで入力するほど座標精度が上がります。",
    )
    submitted = st.form_submit_button("🔍 検索", use_container_width=True, type="primary")

if submitted and address_input.strip():
    clean = re.sub(r"[〒\s]*\d{3}-\d{4}\s*", "", address_input).strip()
    with st.spinner("検索中…"):
        try:
            geo = geocode(clean)
            st.session_state.lat     = geo["lat"]
            st.session_state.lon     = geo["lon"]
            st.session_state.address = geo["normalized"]
            st.session_state.processed_click = None
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
# 地図（常時表示・クリック検索対応）
# ────────────────────────────────────────────────

lat  = st.session_state.lat or 35.6762
lon  = st.session_state.lon or 139.6503
zoom = 16 if st.session_state.lat else 6

m = folium.Map(location=[lat, lon], zoom_start=zoom, tiles=GSI_TILE, attr=GSI_ATTR)

info = st.session_state.get("info", {})

# 用途地域レイヤー
zone_feats = info.get("zone_features", [])
if zone_feats:
    zone_layer = folium.FeatureGroup(name="用途地域 (XKT002)", show=True)
    for feat in zone_feats:
        p     = feat.get("properties", {})
        zname = p.get("use_area_ja", "")
        bcr   = p.get("u_building_coverage_ratio_ja", "")
        far   = p.get("u_floor_area_ratio_ja", "")
        color = ZONE_COLORS.get(zname, "#AAAAAA")
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

# 防火地域レイヤー
fire_feats = info.get("fire_features", [])
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

# 地区計画レイヤー
chiku_feats = info.get("chiku_features", [])
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

# 都市計画道路レイヤー
douro_feats = info.get("douro_features", [])
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

# ────────────────────────────────────────────────
# 地図クリック処理
# ────────────────────────────────────────────────

if map_data and map_data.get("last_clicked"):
    clat = map_data["last_clicked"]["lat"]
    clng = map_data["last_clicked"]["lng"]
    if st.session_state.processed_click != (clat, clng):
        st.session_state.processed_click = (clat, clng)
        st.session_state.lat = clat
        st.session_state.lon = clng
        st.session_state.address = reverse_geocode(clat, clng)
        if api_key:
            st.session_state.info = fetch_planning_info(clat, clng, api_key)
        st.rerun()

# ────────────────────────────────────────────────
# 情報パネル（検索またはクリック後に表示）
# ────────────────────────────────────────────────

if st.session_state.lat and st.session_state.info:
    info = st.session_state.info
    st.divider()

    st.subheader(f"📍 {st.session_state.address}")
    st.caption(f"緯度 {st.session_state.lat:.6f} / 経度 {st.session_state.lon:.6f}")

    na = "⚠️ 要確認"

    # 基本5項目
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("用途地域",    info.get("zone")  or na)
    c2.metric("指定建ぺい率", info.get("bcr")   or na)
    c3.metric("指定容積率",  info.get("far")   or na)
    c4.metric("防火規制",    info.get("fire")  or "指定なし")
    c5.metric("区域区分",    info.get("kubun") or na)

    # 追加項目（値があるものだけ表示）
    extra = [
        ("地区計画",       info.get("chiku")),
        ("高度利用地区",   info.get("koudo")),
        ("立地適正化計画", info.get("tochiseibi")),
        ("都市計画道路",   info.get("douro")),
    ]
    extra_found = [(label, val) for label, val in extra if val]
    if extra_found:
        cols = st.columns(len(extra_found))
        for col, (label, val) in zip(cols, extra_found):
            col.metric(label, val)

    if info.get("errors"):
        st.caption(f"取得エラー（一部データなし）: {', '.join(info['errors'])}")

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

# ────────────────────────────────────────────────
# サイドバー
# ────────────────────────────────────────────────

with st.sidebar:
    st.header("📖 使い方")
    st.markdown("""
**方法①** 住所を入力して「検索」

**方法②** 地図上をクリック

どちらでも都市計画情報を即表示します。

---

**表示レイヤー**

| レイヤー | API | 色 |
|---------|-----|-----|
| 用途地域 | XKT002 | ゾーン別 |
| 防火・準防火 | XKT014 | 赤・橙 |
| 地区計画 | XKT023 | 青緑 |
| 都市計画道路 | XKT030 | 茶 |

---

**取得情報**
- 用途地域・建ぺい率・容積率
- 区域区分（市街化区域等）
- 防火・準防火地域
- 地区計画 / 高度利用地区
- 立地適正化計画（XKT003）
- 都市計画道路（XKT030）

---
⚠️ 本ツールは参考情報です。確認申請・設計判断は行政窓口で確認してください。
""")

    st.header("🔑 API キー")
    if api_key:
        st.success("✅ REINFOLIB_API_KEY 設定済み")
    else:
        st.error("❌ REINFOLIB_API_KEY 未設定")
        st.markdown("[reinfolib API キー取得](https://www.reinfolib.mlit.go.jp/)（無料）")

    st.divider()
    st.caption("データ: reinfolib API (XKT001/002/003/014/023/024/030) 令和6年度 / 地理院タイル")
