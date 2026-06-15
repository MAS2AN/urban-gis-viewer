"""
全国都市計画GISビューア
住所を入力すると都市計画用途地域ポリゴンを地図上に色分け表示し、
reinfolib XKT002(令和6年度) から用途地域・防火規制・地区計画等を取得する。

起動: python -m streamlit run app.py
"""

import os
import re
import sys
from pathlib import Path

import folium
import streamlit as st
from streamlit_folium import st_folium

# Streamlit Cloud Secrets → 環境変数
for _k in ("REINFOLIB_API_KEY",):
    if _k not in os.environ and _k in st.secrets:
        os.environ[_k] = st.secrets[_k]

sys.path.insert(0, str(Path(__file__).parent))
from urban_gis import ZONE_COLORS, fetch_planning_info, geocode

# ────────────────────────────────────────────────
# ページ設定
# ────────────────────────────────────────────────
st.set_page_config(
    page_title="全国都市計画GISビューア",
    page_icon="🗺️",
    layout="wide",
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@300;400;500;700&display=swap');

html, body, .stApp {
    background-color: #F0F4F8 !important;
    font-family: 'Noto Sans JP', sans-serif !important;
    color: #1A2B3C !important;
}
p, span, div, label, li, td, th,
.stMarkdown, [data-testid="stMarkdownContainer"] {
    color: #1A2B3C !important;
}
section[data-testid="stSidebar"] {
    background-color: #E4EBF2 !important;
    border-right: 1px solid #C8D8E8 !important;
}
section[data-testid="stSidebar"] p,
section[data-testid="stSidebar"] span,
section[data-testid="stSidebar"] div,
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] li { color: #1A2B3C !important; }
section[data-testid="stSidebar"] h2 {
    color: #1A4B8C !important; font-size: 1rem !important; font-weight: 700 !important;
}
h1 { color: #1A2B3C !important; font-weight: 700 !important; }
h2, h3 { color: #1A4B8C !important; font-weight: 600 !important; }
[data-testid="stForm"] {
    background: #FFFFFF !important;
    border: 1px solid #C8D8E8 !important;
    border-radius: 14px !important;
    padding: 1.2rem 1.5rem 1rem !important;
    box-shadow: 0 2px 10px rgba(26,75,140,0.08) !important;
}
.stTextInput > div > div > input {
    background: #F8FAFC !important;
    border: 1.5px solid #C8D8E8 !important;
    border-radius: 8px !important;
    color: #1A2B3C !important;
}
.stTextInput > div > div > input:focus {
    border-color: #1A4B8C !important;
    box-shadow: 0 0 0 3px rgba(26,75,140,0.15) !important;
}
.stFormSubmitButton > button {
    background-color: #1A4B8C !important;
    color: #FFFFFF !important;
    border: none !important;
    border-radius: 10px !important;
    font-weight: 600 !important;
    font-size: 1rem !important;
    padding: 0.6rem 2rem !important;
}
.stFormSubmitButton > button:hover { background-color: #163D73 !important; }
[data-testid="stMetric"] {
    background: #FFFFFF !important;
    border: 1px solid #C8D8E8 !important;
    border-radius: 12px !important;
    padding: 1rem 1.2rem !important;
    box-shadow: 0 2px 8px rgba(26,75,140,0.07) !important;
}
[data-testid="stMetricLabel"] { color: #5A7A9C !important; font-size: 0.82rem !important; }
[data-testid="stMetricValue"] { color: #1A2B3C !important; font-weight: 700 !important; }
[data-testid="stAlert"] {
    border-radius: 10px !important; background: #EBF2FA !important;
    border-left-color: #1A4B8C !important;
}
hr { border-color: #C8D8E8 !important; }
</style>
""", unsafe_allow_html=True)

# ────────────────────────────────────────────────
# ヘッダー
# ────────────────────────────────────────────────
st.title("🗺️ 全国都市計画GISビューア")
st.caption(
    "住所を入力すると **用途地域ポリゴン** を地図上に色分け表示します。"
    "データ: reinfolib API XKT002（令和6年度）/ 地理院タイル"
)

# ────────────────────────────────────────────────
# 入力フォーム
# ────────────────────────────────────────────────
with st.form("search_form"):
    address = st.text_input(
        "調査したい住所を入力",
        placeholder="例）東京都千代田区丸の内1-1-1　または　大阪市北区梅田1-1",
        help="番地まで入力するほど座標精度が上がります。",
    )
    submitted = st.form_submit_button("🔍 地図を表示", use_container_width=True, type="primary")

# ────────────────────────────────────────────────
# APIキー確認
# ────────────────────────────────────────────────
api_key = os.environ.get("REINFOLIB_API_KEY", "")
if not api_key:
    st.warning(
        "**REINFOLIB_API_KEY が設定されていません。** "
        "用途地域ポリゴンの取得には reinfolib API キーが必要です。"
        "（[取得はこちら](https://www.reinfolib.mlit.go.jp/)）",
        icon="⚠️",
    )

# ────────────────────────────────────────────────
# 処理・表示
# ────────────────────────────────────────────────
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

        # ── メトリクス ──────────────────────────────
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

        # ── 地図（folium）──────────────────────────
        st.subheader("🗺️ 都市計画 GIS マップ")

        m = folium.Map(
            location=[lat, lon],
            zoom_start=16,
            tiles="https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png",
            attr='<a href="https://maps.gsi.go.jp/development/ichiran.html" target="_blank">地理院タイル (国土地理院)</a>',
        )

        # 用途地域レイヤー（XKT002）
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
                            "fillColor":   c,
                            "color":       "#333333",
                            "weight":      0.8,
                            "fillOpacity": 0.50,
                        },
                        tooltip=folium.Tooltip(tip, sticky=False),
                        popup=folium.Popup(tip, max_width=220),
                    ).add_to(zone_layer)
                except Exception:
                    pass
            zone_layer.add_to(m)

        # 防火地域レイヤー（XKT014）
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

        # 地区計画レイヤー（XKT023）
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

        # 敷地マーカー
        folium.Marker(
            [lat, lon],
            popup=folium.Popup(clean, max_width=250),
            icon=folium.Icon(color="red", icon="home", prefix="glyphicon"),
            tooltip="調査地点",
        ).add_to(m)

        # レイヤーコントロール
        folium.LayerControl(collapsed=False).add_to(m)

        # 地図表示（フル幅・高さ 580px）
        st_folium(m, use_container_width=True, height=580, returned_objects=[])

        st.divider()

        # ── 用途地域 凡例 ─────────────────────────
        st.subheader("🎨 用途地域 凡例")
        legend_html = "<div style='display:flex;flex-wrap:wrap;gap:8px;'>"
        for zname, color in ZONE_COLORS.items():
            legend_html += (
                f"<div style='display:flex;align-items:center;gap:5px;padding:4px 8px;"
                f"background:{color}22;border-left:4px solid {color};border-radius:4px;"
                f"font-size:0.82rem;color:#1A2B3C;'>"
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

# ────────────────────────────────────────────────
# サイドバー
# ────────────────────────────────────────────────
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
        st.markdown(
            "[reinfolib API キー取得](https://www.reinfolib.mlit.go.jp/)（無料）"
        )

    st.divider()
    st.caption(
        "データソース: 国土交通省 不動産情報ライブラリ API (XKT002/014/023/024) "
        "令和6年度 / 地理院タイル (国土地理院)"
    )
