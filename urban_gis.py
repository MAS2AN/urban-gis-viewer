"""
全国都市計画GISビューア ＋ 敷地法規調査 統合ツール
（このファイルが Streamlit Cloud の起動エントリーポイント）

タブ構成:
  Tab 1 - 🗺️ GISビューア    : reinfolib 用途地域ポリゴン・防火・地区計画・道路マップ
  Tab 2 - 📋 法規レポート    : 国土数値情報 A29 + Web検索 による詳細法規チェック
  Tab 3 - 🏗️ ボリューム検討 : 建ぺい率・容積率から建物エンベロープを 3D 可視化
  Tab 4 - 🌊 ハザードマップ  : 国交省ハザードマップポータル XYZ タイル（洪水・土砂・津波・高潮等）
"""

import concurrent.futures as cf
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
from analyze_site import YOTO_DB, build_report, geocode, research, volume_study

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

# ハザードマップポータル XYZ タイル定義
# 出典: 国土交通省ハザードマップポータルサイト（disaportaldata.gsi.go.jp）
_HAZARD_BASE = "https://disaportaldata.gsi.go.jp/raster"
_HAZARD_ATTR = '国土交通省 <a href="https://disaportal.gsi.go.jp/" target="_blank">ハザードマップポータルサイト</a>'

HAZARD_LAYERS = [
    {
        "name": "🌊 洪水浸水想定区域（想定最大規模）",
        "path": "01_flood_l2_shinsuishin_data",
        "show": True,
    },
    {
        "name": "🌊 洪水浸水想定区域（計画規模）",
        "path": "01_flood_l1_shinsuishin_newlegend_data",
        "show": False,
    },
    {
        "name": "💧 内水浸水想定区域",
        "path": "02_naisui_data",
        "show": False,
    },
    {
        "name": "🌊 高潮浸水想定区域（想定最大規模）",
        "path": "03_hightide_l2_shinsuishin_data",
        "show": False,
    },
    {
        "name": "🌊 津波浸水想定",
        "path": "04_tsunami_newlegend_data",
        "show": False,
    },
    {
        "name": "⛰️ 土砂災害警戒区域（土石流）",
        "path": "05_dosekiryukeikaikuiki",
        "show": False,
    },
    {
        "name": "⛰️ 土砂災害警戒区域（急傾斜地の崩壊）",
        "path": "05_kyukeishakeikaikuiki",
        "show": False,
    },
    {
        "name": "⛰️ 土砂災害警戒区域（地すべり）",
        "path": "05_jisuberikeikaikuiki",
        "show": False,
    },
]


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
    """日本語文字（ひらがな・カタカナ・漢字）を含む値だけ返す。内部IDや英数字ハッシュは除外。"""
    return next(
        (v for v in props.values()
         if isinstance(v, str) and any('぀' <= c <= '鿿' for c in v)),
        None,
    )


def _report_to_pdf(report_md: str) -> bytes:
    """Markdown レポートを A4 PDF（バイト列）に変換する。"""
    import markdown as md_lib
    from weasyprint import HTML

    html_body = md_lib.markdown(
        report_md,
        extensions=["tables", "toc"],
    )
    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<style>
@page {{ size: A4; margin: 20mm 18mm; }}
body {{
  font-family: "Noto Sans CJK JP", "Noto Sans JP", "Yu Gothic", "Hiragino Sans", sans-serif;
  font-size: 9.5pt; line-height: 1.65; color: #222;
}}
h1 {{ font-size: 15pt; border-bottom: 2px solid #444; padding-bottom: 4pt; margin-top: 0; }}
h2 {{ font-size: 12pt; border-bottom: 1px solid #bbb; padding-bottom: 2pt; margin-top: 14pt; }}
h3 {{ font-size: 10.5pt; margin-top: 10pt; }}
table {{ border-collapse: collapse; width: 100%; margin: 6pt 0; page-break-inside: avoid; }}
th, td {{ border: 1px solid #ccc; padding: 4pt 7pt; text-align: left; font-size: 8.5pt; }}
th {{ background: #f2f2f2; font-weight: bold; }}
blockquote {{
  border-left: 3px solid #bbb; margin: 6pt 0 6pt 6pt;
  padding: 2pt 0 2pt 10pt; color: #555; font-size: 8.5pt;
}}
ul, ol {{ margin: 4pt 0; padding-left: 18pt; }}
li {{ margin-bottom: 2pt; }}
hr {{ border: none; border-top: 1px solid #ddd; margin: 10pt 0; }}
a {{ color: #1558b0; text-decoration: none; }}
code {{ background: #f5f5f5; padding: 1pt 3pt; border-radius: 2pt; font-size: 8pt; }}
</style>
</head>
<body>
{html_body}
</body>
</html>"""
    return HTML(string=html).write_pdf()


def _xkt030_wide(lat: float, lon: float, api_key: str, grid: int = 3, zoom: int = 14) -> list:
    """XKT030 都市計画道路を grid×grid タイルで並列取得し重複除去して返す。
    zoom14: 1タイル≈2.4km、3×3グリッドで≈7km四方をカバー。
    """
    cx, cy = _lat_lon_to_tile(lat, lon, zoom)
    half = grid // 2
    tiles = [(cx + dx, cy + dy) for dx in range(-half, half + 1) for dy in range(-half, half + 1)]
    headers = {"Ocp-Apim-Subscription-Key": api_key}

    def fetch_tile(tx_ty):
        tx, ty = tx_ty
        try:
            params = {"response_format": "geojson", "z": zoom, "x": tx, "y": ty}
            resp = requests.get(
                f"{REINFOLIB_BASE}/XKT030", headers=headers, params=params, timeout=10
            )
            if resp.status_code == 200:
                return resp.json().get("features", [])
        except Exception:
            pass
        return []

    seen, features = set(), []
    with cf.ThreadPoolExecutor(max_workers=len(tiles)) as pool:
        for tile_feats in pool.map(fetch_tile, tiles):
            for feat in tile_feats:
                key = str(feat.get("geometry", {}).get("coordinates", ""))[:120]
                if key not in seen:
                    seen.add(key)
                    features.append(feat)
    return features


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
        "zone_features": [], "fire_features": [], "chiku_features": [],
        "douro_features": [], "douro_wide_features": [],
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
                or p.get("name") or "計画区域内"
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
                or p.get("name") or "指定区域"
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
                or p.get("zone_type_ja") or "区域内"
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
                or p.get("name_ja") or p.get("name") or "計画区域内"
            )
    except Exception as e:
        result["errors"].append(f"XKT030: {e}")

    try:
        result["douro_wide_features"] = _xkt030_wide(lat, lon, api_key)
    except Exception as e:
        result["errors"].append(f"XKT030_wide: {e}")

    if result["zone"] and not result["kubun"]:
        result["kubun"] = "市街化区域"

    return result


# ────────────────────────────────────────────────
# 3D ボリューム可視化
# ────────────────────────────────────────────────
# 斜線制限 定数・ヘルパー
# ────────────────────────────────────────────────

# 道路斜線係数 1.25 適用ゾーン（住居系）、それ以外は 1.5
_ROAD_SLOPE_RESIDENTIAL = {
    "第一種低層住居専用地域", "第二種低層住居専用地域", "田園住居地域",
    "第一種中高層住居専用地域", "第二種中高層住居専用地域",
    "第一種住居地域", "第二種住居地域", "準住居地域",
}
# 隣地斜線 20m+1.25 適用ゾーン（中高層〜準住居）、それ以外（商業・工業）は 31m+2.5
_ADJ_SLOPE_RESIDENTIAL = {
    "第一種中高層住居専用地域", "第二種中高層住居専用地域",
    "第一種住居地域", "第二種住居地域", "準住居地域",
}
# 北側斜線 基点高さ
_NORTH_SLOPE_LOW = {"第一種低層住居専用地域", "第二種低層住居専用地域", "田園住居地域"}   # 5m
_NORTH_SLOPE_MID = {"第一種中高層住居専用地域", "第二種中高層住居専用地域"}               # 10m


def _shasen_traces(
    zone_name: str, site_w: float, site_d: float, road_width: float | None
) -> list:
    """道路斜線・隣地斜線・北側斜線の制限面トレースを返す。
    座標系: y=0 が前面道路側、y=site_d が北側と仮定。
    建基法第56条第1項各号に基づく斜線係数・起点高さを使用。
    """
    traces = []
    z_data = YOTO_DB.get(zone_name, {})

    # ── 道路斜線制限（第56条第1項第1号）──────────────────────
    if z_data.get("road") and road_width and road_width > 0:
        sf = 1.25 if zone_name in _ROAD_SLOPE_RESIDENTIAL else 1.5
        h0 = road_width * sf              # 前面境界 y=0 での制限高さ
        hd = (site_d + road_width) * sf  # 後方境界 y=site_d での制限高さ

        traces.append(go.Mesh3d(
            x=[0, site_w, site_w, 0],
            y=[0, 0, site_d, site_d],
            z=[h0, h0, hd, hd],
            i=[0, 0], j=[1, 2], k=[2, 3],
            color="#FF7700", opacity=0.13,
            name=f"道路斜線制限（1:{sf}）", showlegend=True,
        ))
        traces.append(go.Scatter3d(
            x=[0, site_w, site_w, 0, 0],
            y=[0, 0, site_d, site_d, 0],
            z=[h0, h0, hd, hd, h0],
            mode="lines", line=dict(color="#FF7700", width=2, dash="dash"),
            showlegend=False, hoverinfo="none",
        ))
        # 道路境界ライン（太め）
        traces.append(go.Scatter3d(
            x=[0, site_w], y=[0, 0], z=[h0, h0],
            mode="lines", line=dict(color="#FF7700", width=4),
            showlegend=False, hoverinfo="none",
        ))
        traces.append(go.Scatter3d(
            x=[site_w * 0.5], y=[site_d * 0.15], z=[h0 + (hd - h0) * 0.15 + 1.5],
            mode="text", text=[f"道路斜線 1:{sf}"],
            textfont=dict(color="#CC5500", size=11),
            showlegend=False, hoverinfo="none",
        ))

    # ── 隣地斜線制限（第56条第1項第2号）──────────────────────
    # 低層住居専用地域・田園住居地域は絶対高さ制限のため非適用
    if z_data.get("adj"):
        is_res = zone_name in _ADJ_SLOPE_RESIDENTIAL
        base_h = 20.0 if is_res else 31.0
        adj_sf = 1.25 if is_res else 2.5
        legend_name = f"隣地斜線制限（{base_h:.0f}m+1:{adj_sf}）"

        # 4 辺それぞれの斜面
        side_args = [
            # 前面 y=0
            ([0, site_w, site_w, 0], [0, 0, site_d, site_d],
             [base_h, base_h, base_h + site_d * adj_sf, base_h + site_d * adj_sf]),
            # 背面 y=site_d
            ([0, site_w, site_w, 0], [site_d, site_d, 0, 0],
             [base_h, base_h, base_h + site_d * adj_sf, base_h + site_d * adj_sf]),
            # 左面 x=0
            ([0, 0, site_w, site_w], [0, site_d, site_d, 0],
             [base_h, base_h, base_h + site_w * adj_sf, base_h + site_w * adj_sf]),
            # 右面 x=site_w
            ([site_w, site_w, 0, 0], [0, site_d, site_d, 0],
             [base_h, base_h, base_h + site_w * adj_sf, base_h + site_w * adj_sf]),
        ]
        for idx, (sx, sy, sz) in enumerate(side_args):
            traces.append(go.Mesh3d(
                x=sx, y=sy, z=sz,
                i=[0, 0], j=[1, 2], k=[2, 3],
                color="#CC0033", opacity=0.07,
                name=legend_name if idx == 0 else "",
                showlegend=(idx == 0),
            ))
        # 起点高さ（base_h）の水平ライン
        traces.append(go.Scatter3d(
            x=[0, site_w, site_w, 0, 0],
            y=[0, 0, site_d, site_d, 0],
            z=[base_h] * 5,
            mode="lines", line=dict(color="#CC0033", width=2, dash="dot"),
            showlegend=False, hoverinfo="none",
        ))
        traces.append(go.Scatter3d(
            x=[site_w * 0.5], y=[site_d * 0.5], z=[base_h + 1.5],
            mode="text", text=[f"隣地斜線起点 {base_h:.0f}m"],
            textfont=dict(color="#CC0033", size=10),
            showlegend=False, hoverinfo="none",
        ))

    # ── 北側斜線制限（第56条第1項第3号）──────────────────────
    # 低層: 5m+1.25 / 中高層: 10m+1.25（日影規制適用時は不適用だが概略表示として描画）
    if zone_name in _NORTH_SLOPE_LOW or zone_name in _NORTH_SLOPE_MID:
        nb = 5.0 if zone_name in _NORTH_SLOPE_LOW else 10.0
        # y=site_d が北側境界：南方向（y=0）へ向かって 1:1.25 で立ち上がる
        h_n = nb                        # 北側境界 y=site_d での制限高さ
        h_s = nb + site_d * 1.25       # 南側 y=0 での制限高さ

        traces.append(go.Mesh3d(
            x=[0, site_w, site_w, 0],
            y=[site_d, site_d, 0, 0],
            z=[h_n, h_n, h_s, h_s],
            i=[0, 0], j=[1, 2], k=[2, 3],
            color="#0055CC", opacity=0.13,
            name=f"北側斜線制限（{nb:.0f}m+1:1.25）", showlegend=True,
        ))
        traces.append(go.Scatter3d(
            x=[0, site_w, site_w, 0, 0],
            y=[site_d, site_d, 0, 0, site_d],
            z=[h_n, h_n, h_s, h_s, h_n],
            mode="lines", line=dict(color="#0055CC", width=2, dash="dash"),
            showlegend=False, hoverinfo="none",
        ))
        # 北側境界ライン（太め）
        traces.append(go.Scatter3d(
            x=[0, site_w], y=[site_d, site_d], z=[h_n, h_n],
            mode="lines", line=dict(color="#0055CC", width=4),
            showlegend=False, hoverinfo="none",
        ))
        traces.append(go.Scatter3d(
            x=[site_w * 0.5], y=[site_d * 0.85], z=[h_n + 1.5],
            mode="text", text=[f"北側斜線 {nb:.0f}m+1:1.25"],
            textfont=dict(color="#0044AA", size=11),
            showlegend=False, hoverinfo="none",
        ))

    return traces


# ────────────────────────────────────────────────

def _create_volume_3d(
    vol: dict, site_w: float, site_d: float,
    road_width: float | None = None,
    show_shasen: bool = True,
) -> go.Figure:
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

    # 斜線制限ラインを追加
    if show_shasen and vol.get("zone_name"):
        traces.extend(_shasen_traces(vol["zone_name"], site_w, site_d, road_width))

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

tab1, tab2, tab3, tab4 = st.tabs(["🗺️ GISビューア", "📋 法規レポート", "🏗️ ボリューム検討", "🌊 ハザードマップ"])

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

    # 都市計画道路 広域レイヤー（XKT030 zoom14 3×3グリッド）
    douro_wide_feats = _info.get("douro_wide_features", [])
    if douro_wide_feats:
        douro_wide_layer = folium.FeatureGroup(
            name=f"都市計画道路 広域（{len(douro_wide_feats)}件）", show=False
        )
        for feat in douro_wide_feats:
            p = feat.get("properties", {})
            dname = (
                p.get("road_name_ja") or p.get("city_planning_road_name_ja")
                or p.get("name_ja") or p.get("name", "都市計画道路")
            )
            try:
                folium.GeoJson(
                    feat,
                    style_function=lambda x: {
                        "color": "#8B0000", "weight": 3.0, "opacity": 0.8,
                        "fillColor": "#8B0000", "fillOpacity": 0.3,
                    },
                    tooltip=folium.Tooltip(dname, sticky=False),
                ).add_to(douro_wide_layer)
            except Exception:
                pass
        douro_wide_layer.add_to(m)

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
            try:
                pdf_bytes = _report_to_pdf(st.session_state.report)
                st.download_button(
                    label="📥 レポートをダウンロード（PDF）",
                    data=pdf_bytes,
                    file_name=f"敷地調査_{safe_name}.pdf",
                    mime="application/pdf",
                    use_container_width=True,
                )
            except Exception as _pdf_err:
                st.warning(f"PDF生成に失敗しました（{_pdf_err}）。Markdownで代替ダウンロードします。")
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
            col_cap, col_chk = st.columns([3, 1])
            with col_cap:
                st.caption(
                    "※敷地を矩形で近似した概算値。前面道路容積率は建基法第52条第2項による。"
                    "　斜線制限は **前方（手前）を前面道路・後方を北側** と仮定した概略表示です（建基法第56条）。"
                )
            with col_chk:
                show_shasen = st.checkbox("📐 斜線制限ラインを表示", value=True)
            fig = _create_volume_3d(vol, site_w, site_d, road_width=road_w, show_shasen=show_shasen)
            st.plotly_chart(fig, use_container_width=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB 4: ハザードマップ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

with tab4:
    _hlat  = st.session_state.lat or 35.6762
    _hlon  = st.session_state.lon or 139.6503
    _hzoom = 14 if st.session_state.lat else 6

    # ポータルサイトへのディープリンク
    portal_url = f"https://disaportal.gsi.go.jp/?ll={_hlat},{_hlon}&z=14&base=pale&vs=c1_z2"
    if st.session_state.lat:
        st.info(
            f"現在の調査地点: **{st.session_state.address}**　"
            f"[🔗 ハザードマップポータルサイトで詳細確認]({portal_url})",
            icon="🌊",
        )
    else:
        st.info("住所を検索するか地図をクリックすると、その地点を中心に表示します。", icon="👆")

    st.caption(
        "⚠️ タイルは国全体の広域データです。市区町村独自の詳細ハザードマップは"
        "各自治体または[ハザードマップポータルサイト](https://disaportal.gsi.go.jp/)で確認してください。"
    )

    # ハザードマップ用 Folium 地図
    hm = folium.Map(location=[_hlat, _hlon], zoom_start=_hzoom, tiles=GSI_TILE, attr=GSI_ATTR)

    # ハザードタイルをレイヤーとして追加
    for layer in HAZARD_LAYERS:
        tile_url = f"{_HAZARD_BASE}/{layer['path']}/{{z}}/{{x}}/{{y}}.png"
        folium.TileLayer(
            tiles=tile_url,
            attr=_HAZARD_ATTR,
            name=layer["name"],
            overlay=True,
            control=True,
            show=layer["show"],
            opacity=0.7,
        ).add_to(hm)

    # 調査地点マーカー
    if st.session_state.lat:
        folium.Marker(
            [st.session_state.lat, st.session_state.lon],
            popup=folium.Popup(st.session_state.address, max_width=250),
            icon=folium.Icon(color="red", icon="home", prefix="glyphicon"),
            tooltip="調査地点",
        ).add_to(hm)

    folium.LayerControl(collapsed=False).add_to(hm)

    st_folium(hm, use_container_width=True, height=580, returned_objects=[])

    st.divider()
    st.subheader("📋 ハザード凡例（色の目安）")
    st.markdown("""
| 色 | 意味（洪水浸水想定区域の場合） |
|----|-------------------------------|
| 🟡 薄黄 | 0.5m 未満 |
| 🟡 黄 | 0.5〜1.0m |
| 🟠 橙 | 1.0〜2.0m |
| 🔴 赤橙 | 2.0〜3.0m |
| 🔴 赤 | 3.0〜5.0m |
| 🟣 濃赤紫 | 5.0〜10.0m |
| 🟤 濃紫 | 10.0〜20.0m |
| ⬛ 黒紫 | 20.0m 以上 |

土砂災害・津波・高潮は各レイヤーで配色が異なります。
詳細は [ハザードマップポータルサイト 凡例ページ](https://disaportal.gsi.go.jp/) を参照してください。
""")


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

→ **ハザードマップ** タブで洪水・土砂・津波・高潮リスクを確認

---

**GISレイヤー（reinfolib API）**

| レイヤー | API | 色 |
|---------|-----|-----|
| 用途地域 | XKT002 | ゾーン別 |
| 防火・準防火 | XKT014 | 赤・橙 |
| 地区計画 | XKT023 | 青緑 |
| 都市計画道路 | XKT030 | 茶 |

---

**ハザードマップレイヤー**

| 種別 | 初期表示 |
|------|---------|
| 洪水（最大規模） | ON |
| 洪水（計画規模） | OFF |
| 内水浸水 | OFF |
| 高潮（最大規模） | OFF |
| 津波浸水 | OFF |
| 土砂（土石流） | OFF |
| 土砂（急傾斜地） | OFF |
| 土砂（地すべり） | OFF |

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
