"""
全国都市計画GISビューア
バックエンドロジック＋Streamlit UI（このファイルが Streamlit Cloud の起動エントリーポイント）
"""

import math
import os
import re
import sys
from pathlib import Path

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
    """緯度経度 → XYZ タイル座標（Web Mercator）"""
    n = 2 ** zoom
    x = int((lon + 180) / 360 * n)
    lat_rad = math.radians(lat)
    y = int((1 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2 * n)
    return x, y


def _reinfolib_features(
    endpoint: str, lat: float, lon: float, api_key: str, zoom: int = _DEFAULT_ZOOM
) -> list[dict]:
    """reinfolib API からタイル内の全フィーチャ（ジオメトリ込み）を返す。"""
    x, y = _lat_lon_to_tile(lat, lon, zoom)
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    params  = {"response_format": "geojson", "z": zoom, "x": x, "y": y}
    resp = requests.get(f"{REINFOLIB_BASE}/{endpoint}", headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get("features", [])


def _hit(features: list[dict], lat: float, lon: float) -> dict | None:
    """フィーチャ列から座標を含む最初のフィーチャを返す。"""
    pt = Point(lon, lat)
    for feat in features:
        try:
            if shape(feat["geometry"]).covers(pt):
                return feat
        except Exception:
            pass
    return None


def geocode(address: str) -> dict:
    """住所 → {lat, lon, normalized, muni_code}"""
    resp = requests.get(GEOCODER_URL, params={"q": address}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError(f"住所が見つかりません: {address}")

    item   = data[0]
    coords = item["geometry"]["coordinates"]
    props  = item["properties"]
    lat    = float(coords[1])
    lon    = float(coords[0])

    muni_code = props.get("muniCode", "")
    if not muni_code:
        try:
            rev = requests.get(REVERSE_GEO_URL, params={"lat": lat, "lon": lon}, timeout=10)
            muni_code = rev.json().get("results", {}).get("muniCd", "")
        except Exception:
            pass

    return {
        "lat":        lat,
        "lon":        lon,
        "normalized": props.get("title", address),
        "muni_code":  muni_code,
    }


def fetch_planning_info(lat: float, lon: float, api_key: str) -> dict:
    """reinfolib API から都市計画情報を一括取得する。"""
    result: dict = {
        "zone": None, "bcr": None, "far": None,
        "fire": None, "kubun": None, "chiku": None, "koudo": None,
        "errors": [],
        "zone_features": [], "fire_features": [], "chiku_features": [],
    }

    try:
        feats = _reinfolib_features("XKT002", lat, lon, api_key)
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
        feats = _reinfolib_features("XKT001", lat, lon, api_key)
        hit = _hit(feats, lat, lon)
        if hit:
            result["kubun"] = hit["properties"].get("area_classification_ja")
    except Exception as e:
        result["errors"].append(f"XKT001: {e}")

    try:
        feats = _reinfolib_features("XKT014", lat, lon, api_key)
        result["fire_features"] = feats
        hit = _hit(feats, lat, lon)
        if hit:
            result["fire"] = hit["properties"].get("fire_prevention_ja")
    except Exception as e:
        result["errors"].append(f"XKT014: {e}")

    try:
        feats = _reinfolib_features("XKT023", lat, lon, api_key)
        result["chiku_features"] = feats
        hit = _hit(feats, lat, lon)
        if hit:
            p = hit["properties"]
            result["chiku"] = (
                p.get("district_plan_name_ja")
                or p.get("name_ja")
                or p.get("name")
                or next((v for v in p.values() if isinstance(v, str) and len(v) > 2), None)
            )
    except Exception as e:
        result["errors"].append(f"XKT023: {e}")

    try:
        feats = _reinfolib_features("XKT024", lat, lon, api_key)
        hit = _hit(feats, lat, lon)
        if hit:
            p = hit["properties"]
            result["koudo"] = (
                p.get("high_use_district_ja")
                or p.get("name_ja")
                or p.get("name")
                or next((v for v in p.values() if isinstance(v, str) and len(v) > 2), None)
            )
    except Exception as e:
        result["errors"].append(f"XKT024: {e}")

    if result["zone"] and not result["kubun"]:
        result["kubun"] = "市街化区域"

    return result


# ────────────────────────────────────────────────
# Streamlit UI
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

st.title("🗺️ 全国都市計画GISビューア")
st.caption(
    "住所を入力すると **用途地域ポリゴン** を地図上に色分け表示します。"
    "データ: reinfolib API XKT002（令和6年度）/ 地理院タイル"
)

with st.form("search_form"):
    address = st.text_input(
        "調査したい住所を入力",
        placeholder="例）東京都千代田区丸の内1-1-1　または　大阪市北区梅田1-1",
        help="番地まで入力するほど座標精度が上がります。",
    )
    submitted = st.form_submit_button("🔍 地図を表示", use_container_width=True, type="primary")

api_key = os.environ.get("REINFOLIB_API_KEY", "")
if not api_key:
    st.warning(
        "**REINFOLIB_API_KEY が設定されていません。** "
        "用途地域ポリゴンの取得には reinfolib API キーが必要です。",
        icon="⚠️",
    )

if submitted and address.strip():
    clean = re.sub(r"[〒\s]*\d{3}-\d{4}\s*", "", address).strip()
    st.divider()

    status = st.status("調査中…", expanded=True)

    try:
        with status:
            st.write("📍 住所を座標に変換中…")
            geo = geocode(clean)
            lat, lon = geo["lat"], geo["lon"]
            st.write(f"　→ 緯度 {lat:.6f} / 経度 {lon:.6f}　（{geo['normalized']}）")

            info: dict = {}
            if api_key:
                st.write("🗂️ reinfolib から都市計画情報を取得中…")
                info = fetch_planning_info(lat, lon, api_key)
                if info.get("zone"):
                    st.write(f"　→ 用途地域: **{info['zone']}**　BCR {info.get('bcr','—')}　FAR {info.get('far','—')}")
                else:
                    st.write("　→ 用途地域のポリゴンが見つかりませんでした（市街化調整区域等の可能性）")

        status.update(label="✅ 取得完了", state="complete", expanded=False)

        na = "⚠️ 要確認"
        st.subheader("📊 都市計画情報")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("用途地域",    info.get("zone")   or na)
        c2.metric("指定建ぺい率", info.get("bcr")    or na)
        c3.metric("指定容積率",  info.get("far")    or na)
        c4.metric("防火規制",    info.get("fire")   or na)
        c5.metric("区域区分",    info.get("kubun")  or na)

        if info.get("chiku") or info.get("koudo"):
            c6, c7 = st.columns(2)
            if info.get("chiku"):
                c6.metric("地区計画", info["chiku"])
            if info.get("koudo"):
                c7.metric("高度利用地区", info["koudo"])

        if info.get("errors"):
            st.caption(f"取得エラー（一部データなし）: {', '.join(info['errors'])}")

        st.divider()

        st.subheader("🗺️ 都市計画 GIS マップ")

        m = folium.Map(
            location=[lat, lon],
            zoom_start=16,
            tiles="https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png",
            attr='<a href="https://maps.gsi.go.jp/development/ichiran.html" target="_blank">地理院タイル (国土地理院)</a>',
        )

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
                            "fillColor": c, "color": "#333333",
                            "weight": 0.8, "fillOpacity": 0.50,
                        },
                        tooltip=folium.Tooltip(tip, sticky=False),
                        popup=folium.Popup(tip, max_width=220),
                    ).add_to(zone_layer)
                except Exception:
                    pass
            zone_layer.add_to(m)

        fire_feats = info.get("fire_features", [])
        if fire_feats:
            fire_layer = folium.FeatureGroup(name="防火・準防火地域 (XKT014)", show=True)
            for feat in fire_feats:
                fname = feat.get("properties", {}).get("fire_prevention_ja", "")
                fcolor = "#FF4444" if "防火地域" in fname and "準" not in fname else "#FF9944"
                try:
                    folium.GeoJson(
                        feat,
                        style_function=lambda x, c=fcolor: {
                            "fillColor": c, "color": c,
                            "weight": 1.5, "fillOpacity": 0.20,
                        },
                        tooltip=folium.Tooltip(fname, sticky=False),
                    ).add_to(fire_layer)
                except Exception:
                    pass
            fire_layer.add_to(m)

        chiku_feats = info.get("chiku_features", [])
        if chiku_feats:
            chiku_layer = folium.FeatureGroup(name="地区計画 (XKT023)", show=False)
            for feat in chiku_feats:
                p = feat.get("properties", {})
                cname = (
                    p.get("district_plan_name_ja")
                    or p.get("name_ja")
                    or p.get("name", "地区計画")
                )
                try:
                    folium.GeoJson(
                        feat,
                        style_function=lambda x: {
                            "fillColor": "#00AAAA", "color": "#007777",
                            "weight": 1.5, "fillOpacity": 0.20,
                        },
                        tooltip=folium.Tooltip(cname, sticky=False),
                    ).add_to(chiku_layer)
                except Exception:
                    pass
            chiku_layer.add_to(m)

        folium.Marker(
            [lat, lon],
            popup=folium.Popup(clean, max_width=250),
            icon=folium.Icon(color="red", icon="home", prefix="glyphicon"),
            tooltip="調査地点",
        ).add_to(m)

        folium.LayerControl(collapsed=False).add_to(m)
        st_folium(m, use_container_width=True, height=580, returned_objects=[])

        st.divider()

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
            "**データについて** — 用途地域・防火地域・地区計画は "
            "国土交通省 不動産情報ライブラリ API（令和6年度）から取得しています。"
            "確認申請・設計判断には必ず所管行政庁の窓口で最新情報を確認してください。",
            icon="ℹ️",
        )

    except ValueError as e:
        status.update(label="❌ エラー", state="error")
        st.error(f"住所が見つかりませんでした: {e}")
    except Exception as e:
        status.update(label="❌ エラー", state="error")
        st.error(f"エラーが発生しました: {e}")

elif submitted:
    st.warning("住所を入力してください。")

with st.sidebar:
    st.header("📖 使い方")
    st.markdown("""
1. 調査したい住所を入力
2. **地図を表示** を押す
3. 色分けポリゴンと情報テーブルが表示されます

---

**表示できるレイヤー**

| レイヤー | API | 色 |
|---------|-----|-----|
| 用途地域 | XKT002 | ゾーン別配色 |
| 防火・準防火地域 | XKT014 | 赤・橙 |
| 地区計画 | XKT023 | 青緑 |

---

**取得できる属性**
- 用途地域・建ぺい率・容積率
- 区域区分（市街化区域等）
- 防火・準防火地域
- 地区計画名称
- 高度利用地区

---

**⚠️ 注意事項**
- 本ツールは参考情報です
- 確認申請・設計判断は行政窓口で確認
""")

    st.header("🔑 API キー")
    if api_key:
        st.success("✅ REINFOLIB_API_KEY 設定済み")
    else:
        st.error("❌ REINFOLIB_API_KEY 未設定")
        st.markdown("[reinfolib API キー取得](https://www.reinfolib.mlit.go.jp/)（無料）")

    st.divider()
    st.caption(
        "データソース: 国土交通省 不動産情報ライブラリ API (XKT002/014/023/024) "
        "令和6年度 / 地理院タイル (国土地理院)"
    )
