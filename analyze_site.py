#!/usr/bin/env python3
"""
敷地法規調査ツール
住所を入力すると都市計画情報・適用法令をまとめたレポートを生成する

一次情報ソース（公式データ）:
  国土数値情報 A29  → 用途地域・容積率・建ぺい率
  reinfolib API     → 防火・準防火地域・区域区分（REINFOLIB_API_KEY 要設定）

二次情報ソース（参考のみ・メインテーブルには反映しない）:
  Gemini / DuckDuckGo → 高度地区・条例等の参考情報

使い方:
    python analyze_site.py "東京都目黒区目黒2-1-1"
    python analyze_site.py "東京都目黒区目黒2-1-1" -o report.md

APIキー取得:
  reinfolib: https://www.reinfolib.mlit.go.jp/ → API利用申請（無料）
  Gemini:    https://aistudio.google.com/apikey（任意）

前提:
    pip install google-genai ddgs beautifulsoup4 requests shapely streamlit
"""

import argparse
import io
import json
import math
import os
import re
import sys
import zipfile
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS

GEOCODER_URL = "https://msearch.gsi.go.jp/address-search/AddressSearch"
REVERSE_GEOCODER_URL = "https://mreversegeocoder.gsi.go.jp/reverse-geocoder/LonLatToAddress"
REINFOLIB_BASE = "https://www.reinfolib.mlit.go.jp/ex-api/external"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0"

CACHE_DIR = Path.home() / ".site_analyzer_cache"
MLIT_A29_URL = "https://nlftp.mlit.go.jp/ksj/gml/data/A29/A29-19/A29-19_{pref_code}_GML.zip"

# N05 都市計画道路: 年度別URLを新しい順に試す
MLIT_N05_URLS = [
    "https://nlftp.mlit.go.jp/ksj/gml/data/N05/N05-22/N05-22_{pref_code}_GML.zip",
    "https://nlftp.mlit.go.jp/ksj/gml/data/N05/N05-21/N05-21_{pref_code}_GML.zip",
    "https://nlftp.mlit.go.jp/ksj/gml/data/N05/N05-19/N05-19_{pref_code}_GML.zip",
    "https://nlftp.mlit.go.jp/ksj/gml/data/N05/N05-18/N05-18_{pref_code}_GML.zip",
]

# N05 整備区分コード → (色, ラベル)
N05_STAGE = {
    1: ("#2244CC", "完成区間"),
    2: ("#FF8800", "工事中"),
    3: ("#CC2200", "計画決定"),
    4: ("#888888", "その他"),
}


def _feature_in_bbox(feat: dict, bbox: tuple) -> bool:
    """GeoJSONフィーチャの座標がbbox内にあるか判定する。"""
    min_lon, min_lat, max_lon, max_lat = bbox
    geom = feat.get("geometry") or {}
    gtype = geom.get("type", "")
    coords = geom.get("coordinates", [])

    def in_box(c):
        return min_lon <= c[0] <= max_lon and min_lat <= c[1] <= max_lat

    if gtype == "Point":
        return in_box(coords)
    if gtype in ("LineString", "MultiPoint"):
        return any(in_box(c) for c in coords)
    if gtype in ("Polygon", "MultiLineString"):
        return any(in_box(c) for ring in coords for c in ring)
    if gtype == "MultiPolygon":
        return any(in_box(c) for poly in coords for ring in poly for c in ring)
    return False


def _n05_stage(props: dict) -> tuple[str, str]:
    """N05 プロパティから整備区分を判定し (hex色, ラベル) を返す。"""
    for key in ("N05_007", "N05_005", "N05_006", "N05_004"):
        v = props.get(key)
        if v is not None:
            try:
                return N05_STAGE.get(int(v), ("#996633", "都市計画道路"))
            except (ValueError, TypeError):
                pass
    return "#996633", "都市計画道路"


def _n05_road_name(props: dict) -> str:
    """N05 プロパティから路線名を返す。"""
    return (
        props.get("N05_003") or props.get("N05_004") or
        props.get("N05_002") or "都市計画道路"
    )


def fetch_n05_roads(
    pref_code: str,
    lat: float,
    lon: float,
    radius_km: float = 2.5,
) -> list:
    """N05 都市計画道路GeoJSONを取得し、指定半径内のフィーチャを返す。

    都道府県単位ZIPをキャッシュし、BBoxフィルタで半径内フィーチャを抽出する。
    Returns: list of GeoJSON Feature dicts（空リストの場合はデータ取得失敗）
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # ── キャッシュ検索 ──────────────────────────
    cache_path = None
    for url_tpl in MLIT_N05_URLS:
        fname = url_tpl.format(pref_code=pref_code).split("/")[-1]
        cp = CACHE_DIR / fname
        if cp.exists():
            cache_path = cp
            print(f"N05 キャッシュ使用: {cp}", file=sys.stderr)
            break

    # ── 未キャッシュ → ダウンロード ──────────────
    if cache_path is None:
        for url_tpl in MLIT_N05_URLS:
            url = url_tpl.format(pref_code=pref_code)
            fname = url.split("/")[-1]
            cp = CACHE_DIR / fname
            try:
                print(f"N05 ダウンロード中: {url}", file=sys.stderr)
                resp = requests.get(url, timeout=180, stream=True)
                resp.raise_for_status()
                with open(cp, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        f.write(chunk)
                cache_path = cp
                print(f"N05 保存完了: {cp} ({cp.stat().st_size // 1024} KB)", file=sys.stderr)
                break
            except Exception as e:
                print(f"N05 失敗 ({url}): {e}", file=sys.stderr)

    if cache_path is None:
        return []

    # ── BBoxフィルタ ──────────────────────────────
    lat_d = radius_km / 111.0
    lon_d = radius_km / (111.0 * math.cos(math.radians(lat)))
    bbox = (lon - lon_d, lat - lat_d, lon + lon_d, lat + lat_d)

    try:
        with zipfile.ZipFile(cache_path) as zf:
            geojson_names = [n for n in zf.namelist() if n.endswith(".geojson")]
            if not geojson_names:
                return []
            features_out = []
            for gname in geojson_names:
                with zf.open(gname) as f:
                    gj = json.load(f)
                for feat in gj.get("features", []):
                    if _feature_in_bbox(feat, bbox):
                        features_out.append(feat)
        print(f"N05 抽出: {len(features_out)} フィーチャ (半径{radius_km}km)", file=sys.stderr)
        return features_out
    except Exception as e:
        print(f"N05 読込失敗: {e}", file=sys.stderr)
        return []

# 国土数値情報 A29 用途地域コード → 名称
A29_YOTO_CODES = {
    1: "第一種低層住居専用地域",
    2: "第二種低層住居専用地域",
    3: "第一種中高層住居専用地域",
    4: "第二種中高層住居専用地域",
    5: "第一種住居地域",
    6: "第二種住居地域",
    7: "準住居地域",
    8: "近隣商業地域",
    9: "商業地域",
    10: "準工業地域",
    11: "工業地域",
    12: "工業専用地域",
    21: "田園住居地域",
}


# ---------------------------------------------------------------------------
# 用途地域ごとの建基法制限データベース
# ---------------------------------------------------------------------------

YOTO_DB = {
    "第一種低層住居専用地域": dict(
        uses="住宅・兼用住宅（兼用部分50㎡以下）・幼稚園〜高校・図書館・神社寺院・診療所（150㎡以内）等",
        max_height="10m または 12m（都市計画による）", abs_height=True,
        road=True, adj=False, north=True, shadow=True,
        shadow_plane="GL+1.5m", shadow_target="軒高7m超または地上3階以上",
    ),
    "第二種低層住居専用地域": dict(
        uses="第一種低層の用途＋小規模店舗（150㎡以内）・飲食店等",
        max_height="10m または 12m（都市計画による）", abs_height=True,
        road=True, adj=False, north=True, shadow=True,
        shadow_plane="GL+1.5m", shadow_target="軒高7m超または地上3階以上",
    ),
    "田園住居地域": dict(
        uses="住宅・農産物直売所・農家レストラン等（農業の利便増進に資するもの）",
        max_height="10m または 12m（都市計画による）", abs_height=True,
        road=True, adj=False, north=True, shadow=True,
        shadow_plane="GL+1.5m", shadow_target="軒高7m超または地上3階以上",
    ),
    "第一種中高層住居専用地域": dict(
        uses="住宅・共同住宅・幼稚園〜大学・病院・診療所・老人ホーム等・500㎡以内の店舗（2階以下）等",
        max_height=None, abs_height=False,
        road=True, adj=True, north=False, shadow=True,
        shadow_plane="GL+4.0m", shadow_target="高さ10m超",
    ),
    "第二種中高層住居専用地域": dict(
        uses="第一種中高層の用途＋1,500㎡以内の店舗・飲食店・オフィス等（2階以下）",
        max_height=None, abs_height=False,
        road=True, adj=True, north=False, shadow=True,
        shadow_plane="GL+4.0m", shadow_target="高さ10m超",
    ),
    "第一種住居地域": dict(
        uses="住宅・共同住宅・3,000㎡以内の店舗・事務所・ホテル等",
        max_height=None, abs_height=False,
        road=True, adj=True, north=False, shadow=True,
        shadow_plane="GL+4.0m", shadow_target="高さ10m超（条例指定の場合のみ）",
    ),
    "第二種住居地域": dict(
        uses="第一種住居の用途＋パチンコ店・カラオケボックス等（床面積制限あり）",
        max_height=None, abs_height=False,
        road=True, adj=True, north=False, shadow=True,
        shadow_plane="GL+4.0m", shadow_target="高さ10m超（条例指定の場合のみ）",
    ),
    "準住居地域": dict(
        uses="住宅系＋自動車関連施設・映画館（200㎡未満）等",
        max_height=None, abs_height=False,
        road=True, adj=True, north=False, shadow=True,
        shadow_plane="GL+4.0m", shadow_target="高さ10m超（条例指定の場合のみ）",
    ),
    "近隣商業地域": dict(
        uses="店舗・飲食店・事務所等に制限なし（危険性の高い工場・風俗施設等を除く）",
        max_height=None, abs_height=False,
        road=True, adj=True, north=False, shadow=True,
        shadow_plane="GL+4.0m", shadow_target="高さ10m超（条例指定の場合のみ）",
    ),
    "商業地域": dict(
        uses="ほぼ全用途可（危険物貯蔵・処理・キャバレー等を除く）",
        max_height=None, abs_height=False,
        road=True, adj=True, north=False, shadow=False,
        shadow_plane=None, shadow_target=None,
    ),
    "準工業地域": dict(
        uses="工場（危険性の高い工場・大規模倉庫を除く）・住宅・店舗・学校等",
        max_height=None, abs_height=False,
        road=True, adj=True, north=False, shadow=True,
        shadow_plane="GL+4.0m", shadow_target="高さ10m超（条例指定の場合のみ）",
    ),
    "工業地域": dict(
        uses="工場中心（住宅可、学校・病院・ホテル等は不可）",
        max_height=None, abs_height=False,
        road=True, adj=True, north=False, shadow=False,
        shadow_plane=None, shadow_target=None,
    ),
    "工業専用地域": dict(
        uses="工場・倉庫・危険物施設（住宅・店舗・学校・病院・ホテル等は建築不可）",
        max_height=None, abs_height=False,
        road=True, adj=True, north=False, shadow=False,
        shadow_plane=None, shadow_target=None,
    ),
}


# ---------------------------------------------------------------------------
# Step 1-b: 国土数値情報 A29 空間クエリ（用途地域・建ぺい率・容積率の一次ソース）
# ---------------------------------------------------------------------------

def _download_and_cache_zip(pref_code: str) -> Path:
    """A29 ZIPをダウンロードしてローカルキャッシュに保存する。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"A29-19_{pref_code}_GML.zip"
    if cache_path.exists():
        print(f"         キャッシュ使用: {cache_path}", file=sys.stderr)
        return cache_path
    url = MLIT_A29_URL.format(pref_code=pref_code)
    print(f"         ダウンロード中: {url}", file=sys.stderr)
    resp = requests.get(url, timeout=180, stream=True)
    resp.raise_for_status()
    with open(cache_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            f.write(chunk)
    print(f"         保存完了: {cache_path} ({cache_path.stat().st_size // 1024} KB)", file=sys.stderr)
    return cache_path


def _find_geojson_in_zip(zf: zipfile.ZipFile, muni_code: str) -> str | None:
    """ZIP内から市区町村コードに対応するGeoJSONファイル名を返す。"""
    names = zf.namelist()

    # パターン1: 完全一致（例: A29-19_13110.geojson）
    for name in names:
        if name.endswith(f"_{muni_code}.geojson") or name.endswith(f"/{muni_code}.geojson"):
            return name

    # パターン2: コードが含まれる .geojson
    for name in names:
        if muni_code in name and name.endswith(".geojson"):
            return name

    # パターン3: 政令指定都市対応
    # 区コード(例:23103)が見つからない場合、数値的に近い親市コード(例:23100)を探す
    # A29データは政令市を市単位ファイル(xxxx00)で格納しているため
    if len(muni_code) == 5 and muni_code.isdigit():
        available: dict[int, str] = {}
        for name in names:
            m = re.search(r'_(\d{5})\.geojson$', name)
            if m:
                available[int(m.group(1))] = name
        muni_int = int(muni_code)
        # 数値差が30以内のファイルを親市コード候補とする
        # 例: 23103(北区)→ 23100(名古屋市), 14131(川崎区)→ 14130(川崎市)
        for code_int, name in sorted(available.items(), key=lambda x: abs(x[0] - muni_int)):
            if abs(code_int - muni_int) <= 30:
                print(f"         政令市フォールバック: {muni_code} → {code_int}", file=sys.stderr)
                return name

    # パターン4: 都道府県単位の単一GeoJSON
    geojsons = [n for n in names if n.endswith(".geojson")]
    if len(geojsons) == 1:
        return geojsons[0]

    return None


def _extract_zone_props(props: dict) -> dict:
    """A29 GeoJSON プロパティから用途地域・建ぺい率・容積率を抽出する。"""
    yoto_code = props.get("A29_004")
    yoto_name = props.get("A29_005") or A29_YOTO_CODES.get(int(yoto_code) if yoto_code is not None else -1, "")
    coverage = props.get("A29_006")
    far = props.get("A29_007")
    result = {}
    if yoto_name:
        result["用途地域"] = str(yoto_name)
    if coverage is not None:
        result["建蔽率"] = f"{coverage}%"
    if far is not None:
        result["容積率"] = f"{far}%"
    return result


def research_spatial(lat: float, lon: float, muni_code: str) -> dict:
    """
    国土数値情報 A29 GeoJSON の点包含クエリで用途地域・建ぺい率・容積率を取得する。
    shapely が未インストールの場合は空dictを返す。
    """
    try:
        from shapely.geometry import Point, shape
    except ImportError:
        print("         ⚠️ shapely 未インストール: pip install shapely", file=sys.stderr)
        return {}

    pref_code = muni_code[:2]
    try:
        zip_path = _download_and_cache_zip(pref_code)
    except Exception as e:
        print(f"         ⚠️ ZIPダウンロード失敗: {e}", file=sys.stderr)
        return {}

    point = Point(lon, lat)  # GeoJSON座標系は (経度, 緯度)

    try:
        with zipfile.ZipFile(zip_path) as zf:
            target = _find_geojson_in_zip(zf, muni_code)
            if not target:
                print(f"         ⚠️ GeoJSON未発見 (muniCode={muni_code})", file=sys.stderr)
                return {}
            with zf.open(target) as f:
                geojson = json.load(f)
    except Exception as e:
        print(f"         ⚠️ GeoJSON読込失敗: {e}", file=sys.stderr)
        return {}

    features = geojson.get("features", [])
    # フィルター要否の判定:
    #   - 完全一致(A29_001 == muni_code): 不要
    #   - 政令市ファイル(A29_001が親市コード): 不要（全区のデータが入っている）
    #   - 都道府県全体ファイル(A29_001が別市): 必要
    if features:
        sample_code = features[0].get("properties", {}).get("A29_001", "")
        try:
            needs_filter = abs(int(sample_code) - int(muni_code)) >= 30
        except (ValueError, TypeError):
            needs_filter = sample_code != muni_code
        if needs_filter:
            features = [f for f in features if f.get("properties", {}).get("A29_001") == muni_code]

    del geojson  # 全量GeoJSONをフィルタリング直後に解放

    print(f"         フィーチャ数: {len(features)}（{muni_code}）", file=sys.stderr)

    # フィーチャごとに形状をキャッシュ（最寄り検索でも使い回す）
    geom_cache: list[tuple] = []
    for feature in features:
        try:
            geom = shape(feature["geometry"])
            geom_cache.append((geom, feature))
        except Exception:
            continue

    # ── Pass 1: 点がポリゴン内部 or 境界上 (covers = contains + boundary) ──
    for geom, feature in geom_cache:
        try:
            if geom.covers(point):
                return _extract_zone_props(feature["properties"])
        except Exception:
            continue

    # ── Pass 2: 最寄りポリゴン（道路上の座標など境界外になるケースに対応）──
    # 閾値 0.001度 ≈ 100m。道路幅を超えることはないため誤爆リスクは低い。
    SNAP_THRESHOLD = 0.001
    min_dist = float("inf")
    nearest_feature = None
    for geom, feature in geom_cache:
        try:
            d = geom.distance(point)
            if d < min_dist:
                min_dist = d
                nearest_feature = feature
        except Exception:
            continue

    if nearest_feature is not None and min_dist <= SNAP_THRESHOLD:
        print(f"         スナップ補正: 最寄りポリゴン距離 {min_dist:.6f}°（道路上の座標を補正）", file=sys.stderr)
        return _extract_zone_props(nearest_feature["properties"])

    # ── 閾値外: 市街化調整区域・非線引き区域・都市計画区域外の可能性 ──
    dist_m = int(min_dist * 111000) if nearest_feature else 0
    print(
        f"         ⚠️ 用途地域ポリゴン外（最近接 {min_dist:.4f}°≈{dist_m}m）\n"
        f"            → 市街化調整区域 / 非線引き区域 / 都市計画区域外 の可能性",
        file=sys.stderr,
    )
    return {"_outside_zone": True}


# ---------------------------------------------------------------------------
# Step 1-c: 不動産情報ライブラリ API（防火規制・区域区分の一次ソース）
# ---------------------------------------------------------------------------
import math as _math

def _lat_lon_to_tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    """緯度経度を XYZ タイル座標に変換する（Web Mercator）。"""
    n = 2 ** zoom
    x = int((lon + 180) / 360 * n)
    lat_rad = _math.radians(lat)
    y = int((1 - _math.log(_math.tan(lat_rad) + 1 / _math.cos(lat_rad)) / _math.pi) / 2 * n)
    return x, y


def _reinfolib_tile(endpoint: str, lat: float, lon: float, api_key: str, zoom: int = 15) -> list[dict]:
    """reinfolib API からタイル内のフィーチャを取得し、指定座標を含むものを返す。"""
    from shapely.geometry import Point, shape

    x, y = _lat_lon_to_tile(lat, lon, zoom)
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    params = {"response_format": "geojson", "z": zoom, "x": x, "y": y}
    resp = requests.get(f"{REINFOLIB_BASE}/{endpoint}", headers=headers, params=params, timeout=15)
    resp.raise_for_status()

    point = Point(lon, lat)
    matches = []
    for feat in resp.json().get("features", []):
        try:
            if shape(feat["geometry"]).covers(point):
                matches.append(feat.get("properties", {}))
        except Exception:
            continue
    return matches


def research_reinfolib(lat: float, lon: float, api_key: str) -> dict:
    """
    不動産情報ライブラリ API（国交省公式）で防火規制・区域区分を取得する。
    取得できた項目は一次情報として扱う。
    """
    result = {}

    # XKT001: 都市計画区域 / 区域区分
    # 複数フィーチャを全走査し「市街化区域」「市街化調整区域」を優先取得する
    try:
        feats = _reinfolib_tile("XKT001", lat, lon, api_key)
        if feats:
            kubun = ""
            # 優先度: 市街化区域(22) > 市街化調整区域(23) > 都市計画区域(21)
            kubun_priority = {"市街化区域": 1, "市街化調整区域": 2}
            best = 99
            for feat in feats:
                v = feat.get("area_classification_ja", "")
                p = kubun_priority.get(v, 10)
                if p < best:
                    best = p
                    kubun = v
            if not kubun:
                kubun = feats[0].get("area_classification_ja", "")
            if kubun:
                result["区域区分"] = kubun
                print(f"         [reinfolib] 区域区分: {kubun}", file=sys.stderr)
    except Exception as e:
        print(f"         ⚠️ reinfolib XKT001 失敗: {e}", file=sys.stderr)

    # XKT014: 防火・準防火地域
    # 注: 法22条区域はXKT014に含まれない（kubun_id 24=防火地域, 25=準防火地域のみ）
    try:
        feats = _reinfolib_tile("XKT014", lat, lon, api_key)
        if feats:
            fire = feats[0].get("fire_prevention_ja", "")
            if fire:
                result["防火規制"] = fire
                print(f"         [reinfolib] 防火規制: {fire}", file=sys.stderr)
        else:
            result["_xkt014_empty"] = True
            print("         [reinfolib] XKT014: 防火・準防火地域の指定なし", file=sys.stderr)
    except Exception as e:
        print(f"         ⚠️ reinfolib XKT014 失敗: {e}", file=sys.stderr)

    # XKT023: 地区計画
    try:
        feats = _reinfolib_tile("XKT023", lat, lon, api_key)
        if feats:
            props = feats[0]
            # 属性名はAPIによって異なるため、文字列値を動的に探す
            name = (
                props.get("district_plan_name_ja")
                or props.get("district_plan_name")
                or props.get("name_ja")
                or props.get("name")
                or next(
                    (v for v in props.values() if isinstance(v, str) and len(v) > 2),
                    "指定あり（名称取得不可）",
                )
            )
            result["地区計画"] = str(name)
            print(f"         [reinfolib] 地区計画: {name}", file=sys.stderr)
        else:
            result["地区計画"] = "なし"
            print("         [reinfolib] XKT023: 地区計画の指定なし", file=sys.stderr)
    except Exception as e:
        print(f"         ⚠️ reinfolib XKT023 失敗: {e}", file=sys.stderr)

    return result


# ---------------------------------------------------------------------------
# Step 1: ジオコーディング
# ---------------------------------------------------------------------------

def geocode(address: str) -> dict:
    lat = lon = None
    normalized = address
    muni_code = ""

    # ── 1st: 国土地理院 ──
    try:
        resp = requests.get(GEOCODER_URL, params={"q": address}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data:
            coords = data[0]["geometry"]["coordinates"]
            props  = data[0]["properties"]
            lat, lon = float(coords[1]), float(coords[0])
            normalized = props.get("title", address)
            muni_code  = props.get("muniCode", "")
    except Exception:
        pass  # → フォールバックへ

    # ── 2nd: Nominatim (OSM) フォールバック ──
    if lat is None:
        try:
            nom_url = "https://nominatim.openstreetmap.org/search"
            nom_resp = requests.get(
                nom_url,
                params={"q": address, "format": "json", "countrycodes": "jp", "limit": 1},
                headers={"User-Agent": "urban-gis-viewer/1.0"},
                timeout=15,
            )
            nom_resp.raise_for_status()
            nom_data = nom_resp.json()
            if not nom_data:
                raise ValueError(f"住所が見つかりません: {address}")
            lat = float(nom_data[0]["lat"])
            lon = float(nom_data[0]["lon"])
            normalized = nom_data[0].get("display_name", address)
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"ジオコーディング失敗（国土地理院・OSM ともに応答なし）: {address}") from e

    # ── 逆ジオコーダーで市区町村コードを取得 ──
    if not muni_code:
        try:
            rev = requests.get(
                REVERSE_GEOCODER_URL,
                params={"lat": lat, "lon": lon},
                timeout=10,
            )
            rev.raise_for_status()
            muni_code = rev.json().get("results", {}).get("muniCd", "")
        except Exception:
            pass

    return {
        "lat": lat,
        "lon": lon,
        "normalized": normalized,
        "muniCode": muni_code,
    }


# ---------------------------------------------------------------------------
# Step 2: Web検索で都市計画情報を取得
# ---------------------------------------------------------------------------

def web_search(queries: list[str], max_each: int = 5) -> list[dict]:
    results = []
    with DDGS() as ddgs:
        for q in queries:
            try:
                hits = list(ddgs.text(q, max_results=max_each, region="jp-jp"))
                results.extend(hits)
            except Exception:
                pass
    # 重複URLを除去
    seen, unique = set(), []
    for r in results:
        url = r.get("href", "")
        if url and url not in seen:
            seen.add(url)
            unique.append(r)
    return unique


def fetch_text(url: str, max_chars: int = 6000) -> str:
    try:
        resp = requests.get(url, timeout=12, headers={"User-Agent": UA})
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        return soup.get_text(separator=" ", strip=True)[:max_chars]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Step 3: テキストから都市計画情報を抽出
# ---------------------------------------------------------------------------

ZONE_NAMES = list(YOTO_DB.keys())


def extract_info(text: str) -> dict:
    info: dict = {}

    # 用途地域（長い名前から順にマッチ）
    if "用途地域" not in info:
        for name in sorted(ZONE_NAMES, key=len, reverse=True):
            if name in text:
                info["用途地域"] = name
                break

    # 建蔽率（60%、60／100 などの表記に対応）
    if "建蔽率" not in info:
        m = re.search(r'建[ぺペ蔽]率[^\d]*(\d+)\s*[%／/]?(?:\s*100)?', text)
        if m:
            v = int(m.group(1))
            info["建蔽率"] = f"{v}%" if v <= 100 else f"{v}%（⚠️ 要確認）"

    # 容積率
    if "容積率" not in info:
        m = re.search(r'容積率[^\d]*(\d+)\s*[%／/]?(?:\s*100)?', text)
        if m:
            v = int(m.group(1))
            info["容積率"] = f"{v}%" if v <= 1000 else f"{v}%（⚠️ 要確認）"

    # 防火規制（準防火地域を先にチェック）
    if "防火規制" not in info:
        if "準防火地域" in text:
            info["防火規制"] = "準防火地域"
        elif "防火地域" in text:
            info["防火規制"] = "防火地域"

    # 高度地区
    if "高度地区" not in info:
        m = re.search(r'第[一二三四1-4]種\s*\d*\s*高度地区|[\d]+m\s*高度地区', text)
        if m:
            info["高度地区"] = m.group(0).strip()

    # 市街化区域・調整区域
    if "区域区分" not in info:
        if "市街化調整区域" in text:
            info["区域区分"] = "市街化調整区域"
        elif "市街化区域" in text:
            info["区域区分"] = "市街化区域"

    return info


def research_gemini(address: str, normalized: str, api_key: str) -> tuple[dict, list[dict], str]:
    """DuckDuckGo検索 → Geminiで構造化解析（グラウンディング不要・完全無料）"""
    from google import genai

    client = genai.Client(api_key=api_key)
    today = date.today().strftime("%Y-%m-%d")

    # Step A: DuckDuckGoで生テキストを収集
    queries = [
        f"{normalized} 用途地域 建蔽率 容積率",
        f"{address} 都市計画 防火地域 高度地区",
    ]
    search_results = web_search(queries, max_each=5)

    collected: list[str] = []
    for r in search_results[:6]:
        snippet = (r.get("title") or "") + " " + (r.get("body") or "")
        collected.append(snippet)
        url = r.get("href", "")
        if url and len(collected) <= 4:
            page = fetch_text(url, max_chars=3000)
            if page:
                collected.append(page)

    web_corpus = "\n---\n".join(collected)[:8000]

    # Step B: Geminiで構造化解析（Web検索なし、テキスト解析のみ）
    prompt = f"""
以下はWeb検索で収集した「{address}」周辺の都市計画に関するテキストです。
このテキストを分析し、対象住所に該当する情報を抽出してください。

【対象住所】{address}（{normalized}）
【調査日】{today}

【Web収集テキスト】
{web_corpus}

---
上記テキストから以下の形式で回答してください。
情報が見つからない場合は「不明」と書いてください。推測・補完禁止。

区域区分: （市街化区域 / 市街化調整区域 / 非線引き区域 / 不明）
用途地域: （正式名称 / 不明）
容積率: （数値% / 不明）
建ぺい率: （数値% / 不明）
防火規制: （防火地域 / 準防火地域 / 指定なし / 不明）
高度地区: （種別・最高高さ / 不明）
日影規制: （制限値・測定水平面 / 不明）
地区計画: （名称 / なし / 不明）
備考: （確認すべき条例・制限など）
"""

    response = client.models.generate_content(
        model="gemini-flash-latest",
        contents=prompt,
    )

    raw_text = response.text or ""

    # Step C: Geminiの回答を構造化
    zone_info: dict = {}

    for name in sorted(ZONE_NAMES, key=len, reverse=True):
        if name in raw_text:
            zone_info["用途地域"] = name
            break

    for label, key, lo, hi in [("容積率", "容積率", 50, 1300), ("建[ぺペ蔽]率", "建蔽率", 30, 80)]:
        if key not in zone_info:
            m = re.search(rf'{label}[^\d]{{0,5}}(\d{{2,4}})\s*[%％]', raw_text)
            if m and lo <= int(m.group(1)) <= hi:
                zone_info[key] = f"{m.group(1)}%"

    if "準防火地域" in raw_text:
        zone_info["防火規制"] = "準防火地域"
    elif re.search(r'(?<!準)防火地域', raw_text):
        zone_info["防火規制"] = "防火地域"

    m = re.search(r'第[一二三四1-4]種\s*\d*\s*高度地区|[\d]+m\s*高度地区', raw_text)
    if m:
        zone_info["高度地区"] = m.group(0).strip()

    if "市街化調整区域" in raw_text and "不明" not in raw_text.split("区域区分")[1][:10] if "区域区分" in raw_text else False:
        zone_info["区域区分"] = "市街化調整区域"
    elif "市街化区域" in raw_text:
        zone_info["区域区分"] = "市街化区域"

    return zone_info, search_results, raw_text


def research_ddgs(address: str, normalized: str) -> tuple[dict, list[dict], str]:
    """DuckDuckGo + スクレイピングで都市計画情報を収集（APIキー不要）"""
    queries = [
        f"{normalized} 用途地域 建蔽率 容積率",
        f"{address} 都市計画 用途地域",
    ]
    search_results = web_search(queries, max_each=5)

    zone_info: dict = {}
    for r in search_results[:8]:
        snippet = (r.get("title") or "") + " " + (r.get("body") or "")
        partial = extract_info(snippet)
        zone_info.update({k: v for k, v in partial.items() if k not in zone_info})

        needs_page = "用途地域" not in zone_info or (
            "建蔽率" not in zone_info and "容積率" not in zone_info
        )
        if needs_page:
            url = r.get("href", "")
            if url:
                page = fetch_text(url)
                partial = extract_info(page)
                zone_info.update({k: v for k, v in partial.items() if k not in zone_info})

        if all(k in zone_info for k in ["用途地域", "建蔽率", "容積率", "防火規制"]):
            break

    return zone_info, search_results, ""


def research(address: str, normalized: str, geo: dict) -> tuple[dict, list[dict], str]:
    """
    ── 情報ソースの優先順位 ──
    一次情報（公式・✅）:
      1. 国土数値情報 A29    → 用途地域・容積率・建ぺい率
      2. reinfolib API       → 防火規制・区域区分（REINFOLIB_API_KEY 必須）
    二次情報（参考のみ・メインテーブルに反映しない）:
      3. Gemini / DuckDuckGo → 高度地区・条例等の参考情報
    """
    lat, lon = geo["lat"], geo["lon"]
    muni_code = geo.get("muniCode", "")
    primary_keys: set[str] = set()
    merged: dict = {}

    # ── 一次情報 1: 国土数値情報 A29（用途地域・容積率・建ぺい率）──
    spatial_info: dict = {}
    if muni_code:
        print("      [1/3] 国土数値情報 A29 空間クエリ...", file=sys.stderr)
        spatial_info = research_spatial(lat, lon, muni_code)

    outside_zone = spatial_info.get("_outside_zone", False)
    if outside_zone:
        merged["_outside_zone"] = True
    else:
        for k, v in spatial_info.items():
            if not k.startswith("_") and v:
                merged[k] = v
                primary_keys.add(k)
        # A29 でゾーンが確定 → 市街化区域も確定（論理的導出）
        if "用途地域" in primary_keys:
            merged["区域区分"] = "市街化区域"
            primary_keys.add("区域区分")
        if merged.get("用途地域"):
            print(
                f"         → 用途地域: {merged['用途地域']} / "
                f"容積率: {merged.get('容積率', '不明')} / "
                f"建ぺい率: {merged.get('建蔽率', '不明')}",
                file=sys.stderr,
            )

    # ── 一次情報 2: reinfolib API（防火規制・区域区分）──
    reinfolib_key = os.environ.get("REINFOLIB_API_KEY", "")
    if reinfolib_key:
        print("      [2/3] reinfolib API（防火規制・区域区分）...", file=sys.stderr)
        try:
            rinfo = research_reinfolib(lat, lon, reinfolib_key)
            for k, v in rinfo.items():
                if not k.startswith("_") and v:
                    # 区域区分: A29 由来の「市街化区域」は reinfolib の「都市計画区域」より優先
                    if k == "区域区分" and merged.get("区域区分") == "市街化区域" and v == "都市計画区域":
                        continue
                    merged[k] = v
                    primary_keys.add(k)

            # 法22条区域の推定:
            # XKT014 が空（防火・準防火の指定なし）かつ A29 でゾーン確定 → 法22条区域の可能性
            # ※ 区域区分の文字列比較はXKT001の境界誤差で誤判定するため、
            #   A29 spatial キーの有無で市街化区域を判定する
            a29_has_zone = "用途地域" in primary_keys and not outside_zone
            if rinfo.get("_xkt014_empty") and "防火規制" not in merged and a29_has_zone:
                merged["防火規制"] = "法22条区域の可能性（要窓口確認）"
                merged["_fire_inferred"] = True  # 推定値フラグ
                print("         → 法22条区域の可能性（A29ゾーン確定かつ防火・準防火の指定なし）", file=sys.stderr)
        except Exception as e:
            print(f"         ⚠️ reinfolib 全体失敗: {e}", file=sys.stderr)
    else:
        print("      [2/3] reinfolib API スキップ（REINFOLIB_API_KEY 未設定）", file=sys.stderr)

    # ── 二次情報: Web検索（参考のみ・メインテーブルには反映しない）──
    google_key = os.environ.get("GOOGLE_API_KEY", "")
    if google_key:
        print("      [3/3] Gemini で Web参考情報を収集中...", file=sys.stderr)
        web_info, search_results, gemini_raw = research_gemini(address, normalized, google_key)
    else:
        print("      [3/3] DuckDuckGo で Web参考情報を収集中...", file=sys.stderr)
        web_info, search_results, gemini_raw = research_ddgs(address, normalized)

    # Web情報はメインデータに混入させず「_web_ref」に格納（レポートの参考欄に表示）
    merged["_web_ref"] = {k: v for k, v in web_info.items() if not k.startswith("_")}
    merged["_spatial_keys"] = primary_keys

    return merged, search_results, gemini_raw


# ---------------------------------------------------------------------------
# ボリューム検討
# ---------------------------------------------------------------------------

# 前面道路容積率の乗数: 住居系=4/10、その他=6/10（建基法第52条第2項）
_RESIDENTIAL_ZONES_FOR_FAR = {
    "第一種低層住居専用地域", "第二種低層住居専用地域", "田園住居地域",
    "第一種中高層住居専用地域", "第二種中高層住居専用地域",
    "第一種住居地域", "第二種住居地域", "準住居地域",
}

# 絶対高さ制限がある用途地域（建基法第55条）
_ABS_HEIGHT_ZONES = {
    "第一種低層住居専用地域", "第二種低層住居専用地域", "田園住居地域",
}


def _parse_percent(s) -> float | None:
    """'200%' → 200.0, None/未取得 → None"""
    if not s:
        return None
    m = re.search(r'(\d+(?:\.\d+)?)', str(s))
    return float(m.group(1)) if m else None


def volume_study(zone_info: dict, site_area: float, road_width: float | None) -> dict:
    """
    敷地面積・前面道路幅員からボリューム検討を行う。

    Returns:
        dict: 計算結果。'error' キーがあれば計算不能を示す。
    """
    import math as _math

    bcr_pct = _parse_percent(zone_info.get("建蔽率"))
    far_pct = _parse_percent(zone_info.get("容積率"))
    zone_name = zone_info.get("用途地域", "")

    if bcr_pct is None:
        return {"error": "建ぺい率が取得できていません。先に住所調査を行ってください。"}
    if far_pct is None:
        return {"error": "容積率が取得できていません。先に住所調査を行ってください。"}

    result: dict = {
        "site_area": site_area,
        "zone_name": zone_name,
        "bcr_pct": bcr_pct,
        "far_pct": far_pct,
    }

    # ── 最大建築面積 ──
    max_building_area = site_area * bcr_pct / 100
    result["max_building_area"] = round(max_building_area, 1)

    # ── 前面道路による容積率制限（建基法第52条第2項）──
    if road_width is not None and road_width > 0:
        is_residential = zone_name in _RESIDENTIAL_ZONES_FOR_FAR
        multiplier = 0.4 if is_residential else 0.6
        road_far_pct = road_width * multiplier * 100
        result["road_far_pct"] = round(road_far_pct, 0)
        result["road_multiplier"] = "4/10（住居系）" if is_residential else "6/10（その他）"
        result["far_limited_by_road"] = road_far_pct < far_pct
        effective_far_pct = min(far_pct, road_far_pct)
    else:
        result["road_far_pct"] = None
        result["far_limited_by_road"] = False
        effective_far_pct = far_pct

    result["effective_far_pct"] = round(effective_far_pct, 0)

    # ── 最大延べ床面積 ──
    max_total_area = site_area * effective_far_pct / 100
    result["max_total_area"] = round(max_total_area, 1)

    # ── 概算階数・最大高さ（階高 3.5m 想定）──
    FLOOR_HEIGHT = 3.5
    est_floors = _math.ceil(max_total_area / max_building_area) if max_building_area > 0 else 1
    result["est_floors"] = est_floors
    result["est_height"] = round(est_floors * FLOOR_HEIGHT, 1)
    result["floor_height"] = FLOOR_HEIGHT

    # ── 絶対高さ制限（低層住居専用地域・建基法第55条）──
    if zone_name in _ABS_HEIGHT_ZONES:
        result["abs_height_limit"] = "10m または 12m（都市計画による）"
        result["abs_height_limited_floors"] = (
            f"{_math.floor(10 / FLOOR_HEIGHT)}〜{_math.floor(12 / FLOOR_HEIGHT)}階"
        )

    return result


# ---------------------------------------------------------------------------
# Step 4: 法令サマリー生成（建基法知識データベース使用）
# ---------------------------------------------------------------------------

def law_summary(zone_name: str, coverage: str, far: str, fire_zone: str) -> str:
    na = "⚠️ 要確認"
    lines: list[str] = []

    # 市街化調整区域等（用途地域なし）の場合
    if "指定なし" in zone_name or "市街化調整区域" in zone_name:
        lines += [
            "> ⚠️ **用途地域の指定がない区域**（市街化調整区域・非線引き区域・都市計画区域外）と判定されました。",
            "",
            "### 市街化調整区域の場合の主な制限",
            "- **用途地域の指定なし** → 用途規制は都市計画法第34条・第35条に基づく開発許可制度が適用",
            "- **建ぺい率・容積率**: 白地地域として建基法第52条・第53条ただし書きによる（自治体指定値）",
            "- **原則として開発行為・建築が制限**される（都市計画法第43条）",
            "- **例外的建築**: 農家住宅・農業用施設・公益施設など限られた用途のみ許可",
            "",
            "### 確認が必要な事項",
            f"- 開発許可の要否（都市計画法第34条各号）",
            f"- 建築許可の要否（都市計画法第43条）",
            f"- 白地地域の建ぺい率・容積率の指定値（自治体担当課）",
        ]
        return "\n".join(lines)

    if zone_name not in YOTO_DB:
        lines.append(f"> ⚠️ 用途地域「{zone_name or '不明'}」が特定できませんでした。行政窓口でご確認ください。")
        return "\n".join(lines)

    z = YOTO_DB[zone_name]

    lines += [
        "### 用途規制（建基法第48条・別表第2）",
        f"主な建築可能用途: {z['uses']}",
        "",
        "### 容積率・建ぺい率（建基法第52条・第53条）",
        f"- 指定容積率: {far or na}",
        f"- 指定建ぺい率: {coverage or na}",
        "",
        "### 斜線制限（建基法第56条）",
        f"- 道路斜線制限: {'**適用あり**（法第56条第1項第1号）' if z['road'] else '適用なし'}",
        f"- 隣地斜線制限: {'**適用あり**（法第56条第1項第2号）' if z['adj'] else '適用なし（低層住居専用地域）'}",
        f"- 北側斜線制限: {'**適用あり**（法第56条第1項第3号）' if z['north'] else '適用なし'}",
    ]

    if z["abs_height"]:
        lines += ["", f"### 絶対高さ制限（建基法第55条）", f"- 最高高さ: **{z['max_height']}**（低層住居専用地域）"]

    lines += ["", "### 日影規制（建基法第56条の2）"]
    if z["shadow"] and z["shadow_plane"]:
        lines += [
            f"- 測定水平面: {z['shadow_plane']}",
            f"- 対象建築物: {z['shadow_target']}",
            f"- 制限値（時間）: {na}（都市計画で個別指定 → 行政窓口で確認）",
        ]
    else:
        lines.append(f"- {zone_name}は日影規制の対象外（または任意指定）")

    lines += ["", "### 防火規制（建基法第22条・第61〜65条）"]
    if fire_zone and "防火地域" in fire_zone and "準" not in fire_zone and "22条" not in fire_zone:
        lines += [
            "- **防火地域**（法第61条）",
            "  - 地上3階以上または延べ床面積100㎡超: 耐火建築物",
            "  - それ以外: 準耐火建築物以上",
        ]
    elif fire_zone and "準防火地域" in fire_zone:
        lines += [
            "- **準防火地域**（法第62条）",
            "  - 地上4階以上または延べ床面積1,500㎡超: 耐火建築物",
            "  - 地上3階以下かつ延べ床面積500㎡超1,500㎡以下: 準耐火建築物",
            "  - 木造の場合は外壁・軒裏を防火構造とする義務",
        ]
    elif fire_zone and "22条" in fire_zone:
        lines += [
            "- ⚠️ **法22条区域の可能性**（要窓口確認）（建基法第22条）",
            "  - 屋根: 不燃材料・準不燃材料で造ること",
            "  - 隣地境界線付近の外壁の開口部: 防火設備等が必要",
            "  - ※防火・準防火地域の指定がないため実際の指定は行政窓口で確認",
        ]
    else:
        lines += [f"- {na}（都市計画GIS または行政窓口で確認）"]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step 5: レポート生成
# ---------------------------------------------------------------------------

def build_report(address: str, geo: dict, zone_info: dict, search_results: list, gemini_raw: str = "") -> str:
    today = date.today().strftime("%Y-%m-%d")

    outside_zone  = zone_info.get("_outside_zone", False)
    spatial_keys  = zone_info.get("_spatial_keys", set())  # A29確定取得キー

    # 信頼度判定: ✅=A29公式 / ⚠️=Web参考 / ✕=未取得
    def _st(key: str, value) -> str:
        if not value or str(value).startswith("⚠️"):
            return "✕"
        return "✅" if key in spatial_keys else "⚠️"

    if outside_zone:
        zone   = "⚠️ 指定なし（市街化調整区域等の可能性）"
        far    = "⚠️ 指定なし"
        cov    = "⚠️ 指定なし"
        kubun  = zone_info.get("区域区分", "市街化調整区域の可能性（要確認）")
    else:
        zone   = zone_info.get("用途地域",  "⚠️ 取得不可（要確認）")
        far    = zone_info.get("容積率",    None)
        cov    = zone_info.get("建蔽率",    None)
        # A29 で用途地域が確定した場合、市街化区域であることが保証される
        # (用途地域は市街化区域または非線引き都市計画区域にのみ指定される)
        if "用途地域" in spatial_keys:
            kubun = "市街化区域"
            spatial_keys = spatial_keys | {"区域区分"}  # ✅ 扱いにする
        else:
            kubun = zone_info.get("区域区分", None)

    # 防火規制: reinfolib確定値なら✅、法22条推定なら⚠️、未取得なら✕
    fire   = zone_info.get("防火規制", None)
    height = zone_info.get("高度地区", None)
    fire_inferred = zone_info.get("_fire_inferred", False)
    if fire_inferred and "防火規制" in spatial_keys:
        spatial_keys = spatial_keys - {"防火規制"}  # 推定値は✅にしない

    na = "⚠️ 要確認"
    law_sec = law_summary(zone, cov, far, fire)

    # Geminiの生テキストがあればセクション追加
    gemini_sec = ""
    if gemini_raw:
        gemini_sec = f"""
---

## 6. Gemini 調査生テキスト（参考）

> Google検索グラウンディングによる生の調査結果です。上記の構造化データに取り込めなかった情報（日影規制の制限値・地区計画・条例等）をここから読み取れます。

{gemini_raw}
"""

    src_lines = "\n".join(
        f"- [{r.get('title') or r.get('href')}]({r.get('href', '')})"
        for r in search_results[:8]
        if r.get("href")
    )

    # Web参考情報セクション（二次情報）
    web_ref = zone_info.get("_web_ref", {})
    LABEL = {"用途地域": "用途地域", "容積率": "容積率", "建蔽率": "建ぺい率",
             "防火規制": "防火規制", "高度地区": "高度地区", "区域区分": "区域区分"}
    web_rows = "\n".join(
        f"| {LABEL.get(k, k)} | {v} |"
        for k, v in web_ref.items()
        if v and not k.startswith("_")
    )
    web_ref_section = (
        f"| 項目 | Web参考値 |\n|------|----------|\n{web_rows}"
        if web_rows else "Web検索からの参考情報は取得できませんでした。"
    )

    # 市区町村名を正規化住所から抽出（条例確認先として表示）
    norm = geo["normalized"]
    m = re.search(r'((?:東京都|.{2,4}[都道府県]).{2,10}[市区町村])', norm)
    jichitai = m.group(1) if m else norm[:8]

    return f"""# 敷地法規調査レポート

| | |
|---|---|
| **対象住所** | {address} |
| **正規化住所** | {norm} |
| **座標** | 緯度 {geo['lat']:.6f} / 経度 {geo['lon']:.6f} |
| **調査日** | {today} |

---

## 1. 都市計画情報（国土数値情報 A29 + Web補完）

| 項目 | 取得値 | 信頼度 |
|------|--------|--------|
| 区域区分 | {kubun or na} | {_st('区域区分', kubun)} |
| 用途地域 | {zone} | {_st('用途地域', zone_info.get('用途地域'))} |
| 指定容積率 | {far or na} | {_st('容積率', far)} |
| 指定建ぺい率 | {cov or na} | {_st('建蔽率', cov)} |
| 防火規制 | {fire or na} | {_st('防火規制', fire)} |
| 高度地区 | {height or na} | {_st('高度地区', height)} |
| 日影規制（数値） | {na} | ✕ |
| 地区計画 | {zone_info.get('地区計画', na)} | {_st('地区計画', zone_info.get('地区計画'))} |

> **凡例** ✅ = 一次情報（国土数値情報・reinfolib公式）　⚠️ = Web参考（要確認）　✕ = 未取得 → **⚠️・✕ は必ず行政窓口 / 都市計画GISで確認してください**
>
> 🔗 ⚠️・✕ 項目は [toshikeikaku-info.jp](https://toshikeikaku-info.jp/) でも手動確認できます（住所を入力して検索）。

---

## 2. 建築基準法 主要制限

{law_sec}

---

## 3. 条例・指導要綱（⚠️ 全項目 要窓口確認）

自動取得不可のため、以下の窓口での確認が必要です。

| 確認項目 | 確認先 |
|---------|--------|
| 中高層建築物紛争予防条例（日照・電波障害等） | {jichitai} 建築指導課 |
| 景観条例・景観計画（高さ・意匠・色彩制限） | 都道府県 / {jichitai} 景観担当課 |
| 福祉のまちづくり条例（BF義務化規模） | 都道府県 福祉まちづくり担当課 |
| 緑化条例（緑化率義務・接道緑化） | {jichitai} 緑化・環境担当課 |
| 建築審査指導要綱・開発指導要綱 | 特定行政庁 建築指導課 |

---

## 4. 確認申請前チェックリスト

- [ ] 用途地域・容積率・建ぺい率を都市計画GISまたは行政窓口で確定
- [ ] 日影規制の制限値（h／4h など）を確認
- [ ] 地区計画・特別用途地区の有無と制限内容を確認
- [ ] 防火・準防火地域の指定を確認
- [ ] 高度地区（最高高さ）の確認
- [ ] 接道する道路の種別（建基法上の道路か・幅員）を確認
- [ ] 2項道路・位置指定道路の場合は後退（セットバック）義務確認
- [ ] 景観計画区域内かどうかの確認
- [ ] 中高層条例の適用規模（高さ10m or 31m以上が多い）の確認

---

## 5. Web参考情報（二次情報・要窓口確認）

> ⚠️ 以下はDuckDuckGo/Gemini Web検索から収集した参考情報です。**住所と無関係な情報が混入する可能性があるため、設計・申請判断には使用しないでください。**

{web_ref_section}

---

## 6. 参照リンク

{src_lines}
{gemini_sec}
---

> **免責事項**: ✅項目は国土数値情報・reinfolib API（国交省公式）から取得した一次情報です。⚠️・✕項目は未取得または二次情報です。確認申請・設計判断に使用する際は必ず所管行政庁の窓口で最新情報を確認してください。
"""


# ---------------------------------------------------------------------------
# CLI エントリーポイント
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="analyze_site",
        description="住所から都市計画情報・適用法令を自動調査する CLI ツール（APIキー不要）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例:\n"
            '  python analyze_site.py "東京都目黒区目黒2-1-1"\n'
            '  python analyze_site.py "東京都目黒区目黒2-1-1" -o report.md'
        ),
    )
    parser.add_argument("address", help="対象敷地の住所")
    parser.add_argument("-o", "--output", metavar="FILE", help="出力先ファイルパス（省略時は標準出力）")
    args = parser.parse_args()

    print(f"[1/4] ジオコーディング中: {args.address}", file=sys.stderr)
    try:
        geo = geocode(args.address)
    except requests.RequestException as e:
        print(f"エラー: 国土地理院APIへの接続に失敗: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"      緯度 {geo['lat']:.6f} / 経度 {geo['lon']:.6f}", file=sys.stderr)
    print(f"      正規化住所: {geo['normalized']}", file=sys.stderr)

    print("[2/4] 都市計画情報を調査中（国土数値情報 + Web）...", file=sys.stderr)
    print("[3/4] 情報を解析中...", file=sys.stderr)
    zone_info, search_results, gemini_raw = research(args.address, geo["normalized"], geo)
    print(f"      取得済み項目: {list(zone_info.keys())}", file=sys.stderr)

    print("[4/4] レポート生成中...", file=sys.stderr)
    report = build_report(args.address, geo, zone_info, search_results, gemini_raw)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"      保存完了: {out.resolve()}", file=sys.stderr)
    else:
        sys.stdout.buffer.write(report.encode("utf-8"))
        sys.stdout.buffer.write(b"\n")


if __name__ == "__main__":
    main()
