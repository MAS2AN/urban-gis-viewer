"""
全国都市計画GISビューア バックエンド
住所 → 座標 → reinfolib XKT002/014/001/023/024 → GeoJSON フィーチャ＋属性テーブル
"""

import math
import os
import re

import requests
from shapely.geometry import Point, shape

# ────────────────────────────────────────────────
# 定数
# ────────────────────────────────────────────────

GEOCODER_URL     = "https://msearch.gsi.go.jp/address-search/AddressSearch"
REVERSE_GEO_URL  = "https://mreversegeocoder.gsi.go.jp/reverse-geocoder/LonLatToAddress"
REINFOLIB_BASE   = "https://www.reinfolib.mlit.go.jp/ex-api/external"

# 用途地域別 標準配色（都市計画の慣例色）
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

# ズームレベルごとのタイル範囲（reinfolib 推奨ズーム）
_DEFAULT_ZOOM = 15


# ────────────────────────────────────────────────
# ユーティリティ
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


# ────────────────────────────────────────────────
# ジオコーディング
# ────────────────────────────────────────────────

def geocode(address: str) -> dict:
    """
    住所 → {lat, lon, normalized, muni_code}
    Raises ValueError if not found.
    """
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


# ────────────────────────────────────────────────
# 都市計画情報取得（reinfolib XKT各種）
# ────────────────────────────────────────────────

def fetch_planning_info(lat: float, lon: float, api_key: str) -> dict:
    """
    reinfolib API から都市計画情報を一括取得する。
    Returns:
        {
          "zone":     str | None,   # 用途地域名
          "bcr":      str | None,   # 建ぺい率
          "far":      str | None,   # 容積率
          "fire":     str | None,   # 防火規制
          "kubun":    str | None,   # 区域区分
          "chiku":    str | None,   # 地区計画
          "koudo":    str | None,   # 高度利用地区
          "errors":   list[str],    # 取得失敗エンドポイント
          "zone_features":  list,   # XKT002 全フィーチャ（マップ用）
          "fire_features":  list,   # XKT014 全フィーチャ（マップ用）
          "chiku_features": list,   # XKT023 全フィーチャ（マップ用）
        }
    """
    result: dict = {
        "zone": None, "bcr": None, "far": None,
        "fire": None, "kubun": None, "chiku": None, "koudo": None,
        "errors": [],
        "zone_features": [], "fire_features": [], "chiku_features": [],
    }

    # XKT002: 用途地域
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

    # XKT001: 区域区分
    try:
        feats = _reinfolib_features("XKT001", lat, lon, api_key)
        hit = _hit(feats, lat, lon)
        if hit:
            result["kubun"] = hit["properties"].get("area_classification_ja")
    except Exception as e:
        result["errors"].append(f"XKT001: {e}")

    # XKT014: 防火・準防火地域
    try:
        feats = _reinfolib_features("XKT014", lat, lon, api_key)
        result["fire_features"] = feats
        hit = _hit(feats, lat, lon)
        if hit:
            result["fire"] = hit["properties"].get("fire_prevention_ja")
    except Exception as e:
        result["errors"].append(f"XKT014: {e}")

    # XKT023: 地区計画
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

    # XKT024: 高度利用地区
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

    # 用途地域が確定したら区域区分は「市街化区域」に確定
    if result["zone"] and not result["kubun"]:
        result["kubun"] = "市街化区域"

    return result
