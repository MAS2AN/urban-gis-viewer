"""
等時間日影・斜線制限・逆日影・逆日影ボリューム 計算モジュール（Streamlit Web UI 用）
updated: 2026-07-08

外部ライブラリ: math のみ（Plotly は呼び出し側でインポート）
計算条件: 冬至日(12/21) / JST 真太陽時補正 / 30分刻み / 2mグリッド
⚠ 概算値（営業・相談段階）確認申請には専用ソフトが必要
exports: solar_position, calc_shadows, suggest_height_solar,
         calc_reverse_shadow, calc_height_limits,
         road_setback_traces, north_setback_traces
"""
import math

TIME_START = 8.0    # 計算開始 (JST)
TIME_END   = 16.0   # 計算終了 (JST)
TIME_STEP  = 0.5    # 時間刻み (h)
GRID_RES   = 2.0    # グリッド解像度 (m)

# 北側斜線: {用途地域名: 立ち上がり高さ(m)}。0 = 適用外。
# 建基法第56条第1項第3号（勾配 1.25 は全区域共通）
NORTH_RISE = {
    "第一種低層住居専用地域": 5, "第二種低層住居専用地域": 5,
    "第一種中高層住居専用地域": 10, "第二種中高層住居専用地域": 10,
}
# 道路斜線勾配（用途地域別）
ROAD_SLOPE = {
    "第一種低層住居専用地域": 1.25,   "第二種低層住居専用地域": 1.25,
    "第一種中高層住居専用地域": 1.25, "第二種中高層住居専用地域": 1.25,
    "第一種住居地域": 1.25,           "第二種住居地域": 1.25,
    "準住居地域": 1.25,               "田園住居地域": 1.25,
    "近隣商業地域": 1.50,             "商業地域": 1.50,
    "準工業地域": 1.50,               "工業地域": 1.50,
    "工業専用地域": 1.50,
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 太陽位置（冬至日固定）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def solar_position(hour_jst: float, lat_deg: float, lon_deg: float = 135.0):
    """冬至日(12/21)の太陽高度(度)・方位角(真北から時計回り、度)を返す。"""
    doy = 355
    B   = 2 * math.pi * (doy - 1) / 365
    eot = 229.18 * (0.000075 + 0.001868*math.cos(B) - 0.032077*math.sin(B)
                    - 0.014615*math.cos(2*B) - 0.04089*math.sin(2*B))
    solar_t = hour_jst + (lon_deg - 135.0) / 15.0 + eot / 60.0
    H    = math.radians(15.0 * (solar_t - 12.0))
    decl = math.radians(-23.45)
    lat  = math.radians(lat_deg)
    sin_alt = math.sin(lat)*math.sin(decl) + math.cos(lat)*math.cos(decl)*math.cos(H)
    alt  = math.asin(max(-1.0, min(1.0, sin_alt)))
    az   = (math.atan2(math.sin(H),
                       math.cos(H)*math.sin(lat) - math.tan(decl)*math.cos(lat))
            + math.pi) % (2*math.pi)
    return math.degrees(alt), math.degrees(az)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 座標変換: 地理 → プロット
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _geo_to_plot(e_geo: float, n_geo: float, road_bearing_deg: float):
    """
    地理座標(east, north) → プロット座標(x, y)。
    プロット +Y 方向 = 道路と反対方向（敷地奥行き方向）。
    road_bearing_deg: 前面道路の方角(0=北、90=東、180=南、270=西)
    """
    beta = math.radians((road_bearing_deg + 180) % 360)
    return (e_geo * math.cos(beta) - n_geo * math.sin(beta),
            e_geo * math.sin(beta) + n_geo * math.cos(beta))

def _north_in_plot(road_bearing_deg: float):
    """真北方向をプロット座標の単位ベクトルで返す。"""
    return _geo_to_plot(0, 1, road_bearing_deg)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ジオメトリ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _convex_hull(pts):
    pts = sorted(set((round(x, 3), round(y, 3)) for x, y in pts))
    if len(pts) <= 2: return pts
    def cross(O, A, B): return (A[0]-O[0])*(B[1]-O[1])-(A[1]-O[1])*(B[0]-O[0])
    lo, hi = [], []
    for p in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], p) <= 0: lo.pop()
        lo.append(p)
    for p in reversed(pts):
        while len(hi) >= 2 and cross(hi[-2], hi[-1], p) <= 0: hi.pop()
        hi.append(p)
    return lo[:-1] + hi[:-1]

def _in_poly(px, py, poly):
    n = len(poly); inside = False; j = n - 1
    for i in range(n):
        xi, yi = poly[i]; xj, yj = poly[j]
        if ((yi > py) != (yj > py)) and px < (xj-xi)*(py-yi)/(yj-yi)+xi:
            inside = not inside
        j = i
    return inside

def _in_rect(px, py, w, d, tol=0.05):
    return -tol <= px <= w+tol and -tol <= py <= d+tol

def _triangulate(n):
    """凸 n 角形の扇形三角形分解 (fan from vertex 0)。"""
    return [0]*(n-2), list(range(1,n-1)), list(range(2,n))

def _grid_mesh3d(pts_xy, z_m, grid_res, color, opacity, name, showlegend=True):
    """グリッド点集合を薄い Mesh3d（1セル=2三角形）に変換して返す。"""
    import plotly.graph_objects as go
    if not pts_xy: return None
    half = grid_res / 2
    vx, vy, vz, ii, jj, kk = [], [], [], [], [], []
    for gx, gy in pts_xy:
        b = len(vx)
        vx.extend([gx-half, gx+half, gx+half, gx-half])
        vy.extend([gy-half, gy-half, gy+half, gy+half])
        vz.extend([z_m]*4)
        ii.extend([b, b]); jj.extend([b+1, b+2]); kk.extend([b+2, b+3])
    return go.Mesh3d(
        x=vx, y=vy, z=vz, i=ii, j=jj, k=kk,
        color=color, opacity=opacity, name=name,
        showlegend=showlegend, hoverinfo="none",
    )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1時刻の影ポリゴン
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _shadow_at(fp, height_m, meas_h_m, alt_deg, az_geo_deg, road_bearing_deg):
    """1時刻の影ポリゴン（プロット座標, convex hull）。高度 2° 未満は None。"""
    if alt_deg < 2.0: return None
    alt = math.radians(alt_deg)
    L   = max(0, height_m - meas_h_m) / math.tan(alt)
    # 地理座標での影オフセット（太陽方向の逆）
    dx_geo = -L * math.sin(math.radians(az_geo_deg))
    dy_geo = -L * math.cos(math.radians(az_geo_deg))
    dx_plot, dy_plot = _geo_to_plot(dx_geo, dy_geo, road_bearing_deg)
    tips = [(x+dx_plot, y+dy_plot) for x, y in fp]
    return _convex_hull(list(fp) + tips)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# メイン: 等時間日影計算
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def calc_shadows(
    bldg_fp: list,
    height_m: float,
    meas_h_m: float,
    lat_deg: float,
    lon_deg: float,
    road_bearing_deg: float,
    threshold_h: float,
    site_w: float,
    site_d: float,
    grid_res: float = GRID_RES,
) -> dict:
    """
    等時間日影を計算して Plotly トレースと統計を返す。

    引数:
      bldg_fp: 建物フットプリント [(x,y), ...] (プロット座標, m)
      height_m: 建物高さ (m)
      meas_h_m: 測定面高さ (m)
      lat_deg, lon_deg: 敷地の緯度・経度
      road_bearing_deg: 前面道路の方角(0=北, 90=東, 180=南, 270=西)
      threshold_h: 日影規制時間 (h)
      site_w, site_d: 敷地の幅・奥行 (m)
      grid_res: グリッド解像度 (m)

    戻り値:
      {traces, violation, iso_area_m2, violation_area_m2, max_extent_m}
    """
    import plotly.graph_objects as go

    # 各時刻の影ポリゴンとbboxキャッシュ
    sp_items = []
    all_pts  = list(bldg_fp)
    times = []
    t = TIME_START
    while t <= TIME_END + 1e-9: times.append(t); t += TIME_STEP

    for hr in times:
        alt_d, az_d = solar_position(hr, lat_deg, lon_deg)
        sp = _shadow_at(bldg_fp, height_m, meas_h_m, alt_d, az_d, road_bearing_deg)
        if sp:
            xs = [x for x,y in sp]; ys = [y for x,y in sp]
            sp_items.append((sp, min(xs), max(xs), min(ys), max(ys)))
            all_pts.extend(sp)
        else:
            sp_items.append(None)

    # グリッド範囲
    if all_pts:
        xs = [x for x,y in all_pts]; ys = [y for x,y in all_pts]
        x0 = math.floor(min(xs)/grid_res)*grid_res - grid_res
        x1 = math.ceil(max(xs)/grid_res)*grid_res  + grid_res
        y0 = math.floor(min(ys)/grid_res)*grid_res - grid_res
        y1 = math.ceil(max(ys)/grid_res)*grid_res  + grid_res
    else:
        return {"traces": [], "violation": False,
                "iso_area_m2": 0, "violation_area_m2": 0, "max_extent_m": 0}

    # グリッド計算
    iso_in_site, iso_out_site = [], []
    union_pts = list(all_pts)
    max_ext = 0.0

    gx = x0
    while gx <= x1 + 1e-6:
        gy = y0
        while gy <= y1 + 1e-6:
            count = sum(
                1 for item in sp_items
                if item and item[1] <= gx <= item[2] and item[3] <= gy <= item[4]
                   and _in_poly(gx, gy, item[0])
            )
            hrs = count * TIME_STEP
            if hrs >= threshold_h - 1e-6:
                if _in_rect(gx, gy, site_w, site_d):
                    iso_in_site.append((gx, gy))
                else:
                    iso_out_site.append((gx, gy))
                    ext = max(
                        max(-gx, 0), max(gx - site_w, 0),
                        max(-gy, 0), max(gy - site_d, 0),
                    )
                    if ext > max_ext: max_ext = ext
            gy += grid_res
        gx += grid_res

    traces = []

    # 全日影ユニオン（水色）
    if len(union_pts) >= 3:
        hull = _convex_hull(union_pts)
        if len(hull) >= 3:
            ii, jj, kk = _triangulate(len(hull))
            traces.append(go.Mesh3d(
                x=[p[0] for p in hull], y=[p[1] for p in hull],
                z=[meas_h_m]*len(hull),
                i=ii, j=jj, k=kk,
                color="#5BA9CF", opacity=0.20,
                name="全日影ユニオン（8〜16時）",
                showlegend=True, hoverinfo="none",
            ))

    # 等時間ゾーン（敷地内）オレンジ
    m = _grid_mesh3d(iso_in_site, meas_h_m + 0.1, grid_res,
                     "#E6781E", 0.45, f"等時間 ≥{threshold_h}h（敷地内）")
    if m: traces.append(m)

    # 違反ゾーン（敷地外）赤
    m = _grid_mesh3d(iso_out_site, meas_h_m + 0.2, grid_res,
                     "#CC2222", 0.70, f"⚠️ 違反 ≥{threshold_h}h（敷地外）")
    if m: traces.append(m)

    # 敷地境界強調（測定面高さで赤枠）
    if iso_out_site:
        traces.append(go.Scatter3d(
            x=[0, site_w, site_w, 0, 0],
            y=[0, 0, site_d, site_d, 0],
            z=[meas_h_m + 0.05]*5,
            mode="lines",
            line=dict(color="#CC0000", width=4),
            name="敷地境界（測定面）", showlegend=True, hoverinfo="none",
        ))

    return {
        "traces": traces,
        "violation": len(iso_out_site) > 0,
        "iso_area_m2": (len(iso_in_site) + len(iso_out_site)) * grid_res**2,
        "violation_area_m2": len(iso_out_site) * grid_res**2,
        "max_extent_m": max_ext,
    }

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 逆日影計算
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _clip_fp_to_yband(bldg_fp, y_lo, y_hi):
    """
    建物フットプリントと水平帯 y_lo ≤ y ≤ y_hi の交差部分を
    矩形フットプリント [(x_lo,y_lo),(x_hi,y_lo),(x_hi,y_hi),(x_lo,y_hi)] として返す。
    交差なし・幅/高さが小さすぎる場合は None。
    """
    xs_in_band = []
    n = len(bldg_fp)
    for i in range(n):
        x1, y1 = bldg_fp[i]
        x2, y2 = bldg_fp[(i + 1) % n]
        # 帯内にある頂点
        if y_lo - 1e-9 <= y1 <= y_hi + 1e-9:
            xs_in_band.append(x1)
        # 辺と y=y_lo, y=y_hi の交点
        dy_edge = y2 - y1
        if abs(dy_edge) > 1e-9:
            for y_cut in (y_lo, y_hi):
                t = (y_cut - y1) / dy_edge
                if -1e-9 <= t <= 1.0 + 1e-9:
                    xs_in_band.append(x1 + t * (x2 - x1))
    if len(xs_in_band) < 2:
        return None
    x_lo, x_hi = min(xs_in_band), max(xs_in_band)
    if x_hi - x_lo < 0.5 or y_hi - y_lo < 0.5:
        return None
    return [(x_lo, y_lo), (x_hi, y_lo), (x_hi, y_hi), (x_lo, y_hi)]


def _ray_to_polygon_entry(px, py, dx, dy, polygon):
    """
    点(px,py)から方向(dx,dy)へのレイが凸多角形に入る最小距離 t≥0 を返す。
    交差しなければ None。
    """
    min_t = None
    n = len(polygon)
    for i in range(n):
        ax, ay = polygon[i]
        bx, by = polygon[(i + 1) % n]
        edx, edy = bx - ax, by - ay
        denom = dx * edy - dy * edx
        if abs(denom) < 1e-9:
            continue
        t = ((ax - px) * edy - (ay - py) * edx) / denom
        s = ((ax - px) * dy  - (ay - py) * dx)  / denom
        if t >= -1e-6 and -1e-6 <= s <= 1.0 + 1e-6:
            t = max(0.0, t)
            if min_t is None or t < min_t:
                min_t = t
    return min_t


def calc_volume_from_shadow(
    bldg_fp: list,
    meas_h_m: float,
    lat_deg: float,
    lon_deg: float,
    road_bearing_deg: float,
    threshold_h: float,
    site_w: float,
    site_d: float,
    grid_res: float = 2.0,
    margin: float = 30.0,  # unused (kept for API compatibility)
) -> dict:
    """
    逆日影ボリューム（スライス×バイナリサーチ方式）

    【旧版の問題点】
    - 各グリッド点を独立した「柱」として扱い、「その1点の影が境界まで届く最小高さ」を
      推定していた。実際の建物は連続した固体であり、建物全体の影を考慮すべき。
    - 測定点が敷地境界上（正しくは境界外）であった。
    - 境界距離 d_bnd は直線距離のみで、建物幅方向の影の広がりを無視していた。

    【新アルゴリズム】
    1. suggest_height_solar で建物全体の均一最大高さ H_uniform を求める（基準値）。
    2. フットプリントを y 方向 N_BAND 本のスライスに分割。
    3. 各スライスの矩形フットプリントで suggest_height_solar（内部で calc_shadows）を
       バイナリサーチ → そのスライス単体の最大許容高さ H_s を決定。
       「スライス単体」の評価なので建物前面側のスライスは影が遠く → 高さ余裕大、
       北側（敷地境界寄り）のスライスは影が境界にすぐ届く → 高さ制限厳しい。
    4. 各グリッドセルに属するスライスの H_s を割り当て → 高さマップを構築。
    5. 既存の Mesh3d 生成ロジックで 3D バー表示。

    計算回数: H_uniform(12回) + N_BAND×12回 ≈ 72回 の calc_shadows（grid_res=4m）。

    Returns: {traces, height_map: {(gx,gy): h_max}}
    """
    import plotly.graph_objects as go

    N_BAND = 5  # y方向スライス数（精度と速度のバランス）

    # ── フットプリント BBox ──
    xs_fp = [p[0] for p in bldg_fp]
    ys_fp = [p[1] for p in bldg_fp]
    fp_x0, fp_x1 = min(xs_fp), max(xs_fp)
    fp_y0, fp_y1 = min(ys_fp), max(ys_fp)
    band_h = (fp_y1 - fp_y0) / N_BAND if N_BAND > 0 else 1.0

    # ── Step 1: 建物全体の均一最大高さ H_uniform ──
    H_uniform = suggest_height_solar(
        bldg_fp, 60.0, meas_h_m,
        lat_deg, lon_deg, road_bearing_deg,
        threshold_h, site_w, site_d,
    )
    if H_uniform <= 0:
        # 60m でも適合不可 → 非常に制約の強い敷地。フォールバック値で継続
        H_uniform = meas_h_m + 2.0

    # ── Step 2: y方向スライスごとにバイナリサーチ ──
    strip_heights = []
    for s in range(N_BAND):
        y_lo = fp_y0 + s * band_h
        y_hi = fp_y0 + (s + 1) * band_h

        strip_fp = _clip_fp_to_yband(bldg_fp, y_lo, y_hi)
        if strip_fp is None:
            # スライスが建物外 → 全体の均一高さで代替
            strip_heights.append(H_uniform)
            continue

        h_s = suggest_height_solar(
            strip_fp, H_uniform, meas_h_m,
            lat_deg, lon_deg, road_bearing_deg,
            threshold_h, site_w, site_d,
        )
        if h_s <= 0:
            h_s = meas_h_m + 1.0  # 極端に低い → 最低限に設定
        strip_heights.append(h_s)

    # ── Step 3: height_map 構築 ──
    height_map = {}
    gx = fp_x0
    while gx <= fp_x1 + 1e-6:
        gy = fp_y0
        while gy <= fp_y1 + 1e-6:
            if _in_poly(gx, gy, bldg_fp):
                s_idx = int((gy - fp_y0) / band_h) if band_h > 0 else 0
                s_idx = max(0, min(s_idx, N_BAND - 1))
                height_map[(gx, gy)] = strip_heights[s_idx]
            gy += grid_res
        gx += grid_res

    if not height_map:
        return {"traces": [], "height_map": {}}

    # ── Step 4: Mesh3d 生成（既存ロジック流用） ──
    s_cell = grid_res / 2 * 0.88  # セル半辺（隙間あり）
    h_vals_all = list(height_map.values())
    h_lo = max(0.0, min(h_vals_all))
    h_hi = max(h_vals_all)

    vx_all, vy_all, vz_all, intens_all = [], [], [], []
    ii_all, jj_all, kk_all = [], [], []
    v_off = 0

    for (cgx, cgy), h in height_map.items():
        h = max(0.1, h)
        vx_all.extend([cgx-s_cell, cgx+s_cell, cgx+s_cell, cgx-s_cell,
                        cgx-s_cell, cgx+s_cell, cgx+s_cell, cgx-s_cell])
        vy_all.extend([cgy-s_cell, cgy-s_cell, cgy+s_cell, cgy+s_cell,
                        cgy-s_cell, cgy-s_cell, cgy+s_cell, cgy+s_cell])
        vz_all.extend([0, 0, 0, 0, h, h, h, h])
        intens_all.extend([h] * 8)

        b = v_off
        ii_all += [b+4, b+4]; jj_all += [b+5, b+6]; kk_all += [b+6, b+7]
        for i0, i1, i2, i3 in [(0,1,5,4),(1,2,6,5),(2,3,7,6),(3,0,4,7)]:
            ii_all += [b+i0, b+i1]; jj_all += [b+i1, b+i2]; kk_all += [b+i2, b+i3]
        v_off += 8

    traces = [go.Mesh3d(
        x=vx_all, y=vy_all, z=vz_all,
        i=ii_all, j=jj_all, k=kk_all,
        intensity=intens_all,
        intensitymode="vertex",
        colorscale=[[0.0,"#2C6BA4"],[0.4,"#F5D020"],[0.75,"#E6781E"],[1.0,"#CC2222"]],
        cmin=h_lo, cmax=h_hi,
        showscale=True,
        colorbar=dict(title="最大高さ(m)", thickness=14, len=0.55, x=1.02),
        opacity=0.85,
        name="逆日影ボリューム",
        showlegend=True,
        hovertemplate="X:%{x:.0f}m Y:%{y:.0f}m<br>許容最大高さ:%{z:.1f}m<extra></extra>",
    )]

    return {"traces": traces, "height_map": height_map}


def calc_reverse_shadow(
    bldg_fp: list,
    meas_h_m: float,
    lat_deg: float,
    lon_deg: float,
    road_bearing_deg: float,
    threshold_h: float,
    site_w: float,
    site_d: float,
    grid_res: float = 4.0,
    margin: float = 30.0,
) -> dict:
    """
    逆日影計算: 各測定点P で「ちょうどN時間日影になる建物の最小高さ」を求める。

    通常の日影: 建物高さH → 影の到達範囲
    逆日影:     測定点P → 「P がN時間影になる最小建物高さ」

    色の意味:
      赤（低H）= 短い建物でも N時間影になる敏感な点
      緑（高H）= かなり高くしないとN時間影にならない余裕ある点

    戻り値: {traces, min_h（全測定点中の最小逆日影H）}
    """
    import plotly.graph_objects as go

    # 時刻ごとの太陽位置・影方向（プロット座標）を計算
    times = []
    t_cur = TIME_START
    while t_cur <= TIME_END + 1e-9:
        times.append(t_cur); t_cur += TIME_STEP

    sun_data = []
    for hr in times:
        alt_d, az_d = solar_position(hr, lat_deg, lon_deg)
        if alt_d < 2.0:
            sun_data.append(None); continue
        geo_dx = -math.sin(math.radians(az_d))
        geo_dy = -math.cos(math.radians(az_d))
        pdx, pdy = _geo_to_plot(geo_dx, geo_dy, road_bearing_deg)
        sun_data.append((math.radians(alt_d), pdx, pdy))

    n_steps_needed = max(1, int(round(threshold_h / TIME_STEP)))

    # グリッド範囲（敷地＋周辺 margin m）
    x0 = -margin;    x1 = site_w + margin
    y0 = -margin;    y1 = site_d + margin

    pts_x, pts_y, pts_h = [], [], []
    min_h = None

    gx = x0
    while gx <= x1 + 1e-6:
        gy = y0
        while gy <= y1 + 1e-6:
            # 建物フットプリント内は計算対象外
            if _in_poly(gx, gy, bldg_fp):
                gy += grid_res; continue

            h_values = []
            for sd in sun_data:
                if sd is None: continue
                alt_r, pdx, pdy = sd
                # 測定点 P から「太陽方向 (-pdx,-pdy)」にレイを飛ばし建物に当たる距離を求める
                L = _ray_to_polygon_entry(gx, gy, -pdx, -pdy, bldg_fp)
                if L is not None and L > 1e-3:
                    h_values.append(meas_h_m + L * math.tan(alt_r))

            h_values.sort()
            if len(h_values) >= n_steps_needed:
                h_rev = h_values[n_steps_needed - 1]
                if h_rev < 200:  # 非現実的な高さは除外
                    pts_x.append(gx); pts_y.append(gy); pts_h.append(h_rev)
                    if min_h is None or h_rev < min_h:
                        min_h = h_rev
            gy += grid_res
        gx += grid_res

    traces = []
    if pts_h:
        h_lo = max(0, min(pts_h))
        h_hi = min(60, max(pts_h))
        traces.append(go.Scatter3d(
            x=pts_x, y=pts_y, z=[meas_h_m] * len(pts_x),
            mode="markers",
            marker=dict(
                size=max(3, int(grid_res * 1.8)),
                color=pts_h,
                colorscale=[
                    [0.0,  "#CC2222"],
                    [0.25, "#E6781E"],
                    [0.55, "#F5D020"],
                    [1.0,  "#3CB371"],
                ],
                cmin=h_lo, cmax=h_hi,
                colorbar=dict(title="逆日影H(m)", thickness=14, len=0.55, x=1.02),
                opacity=0.80,
            ),
            name=f"逆日影高さ（{threshold_h}時間）",
            showlegend=True,
            hovertemplate="X:%{x:.0f}m Y:%{y:.0f}m<br>逆日影H:%{marker.color:.1f}m<extra></extra>",
        ))

    return {"traces": traces, "min_h": min_h}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 推奨高さ（バイナリサーチ）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _has_violation(bldg_fp, height_m, meas_h_m, lat, lon, bearing, thresh, site_w, site_d):
    """4m グリッドで違反点があるか（高速チェック用）。"""
    res = calc_shadows(bldg_fp, height_m, meas_h_m, lat, lon, bearing, thresh,
                       site_w, site_d, grid_res=4.0)
    return res["violation"]

def suggest_height_solar(
    bldg_fp, height_m, meas_h_m, lat, lon, bearing, thresh, site_w, site_d
):
    """
    規制内に収まる最大建物高さをバイナリサーチで返す。
    収まらない場合は -1。
    """
    if not _has_violation(bldg_fp, height_m, meas_h_m, lat, lon, bearing, thresh, site_w, site_d):
        return height_m  # 既に適合
    lo, hi = meas_h_m + 1.0, height_m
    if _has_violation(bldg_fp, lo, meas_h_m, lat, lon, bearing, thresh, site_w, site_d):
        return -1.0  # 最低限まで下げても超過
    for _ in range(10):
        mid = (lo + hi) / 2.0
        if _has_violation(bldg_fp, mid, meas_h_m, lat, lon, bearing, thresh, site_w, site_d):
            hi = mid
        else:
            lo = mid
    return round(lo, 1)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 斜線制限: 許容高さ計算
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def road_setback_limit(road_w_m: float, setback_m: float, zone_name: str) -> float:
    """道路斜線制限による高さ上限（道路境界から setback_m 後退した位置）。"""
    slope = ROAD_SLOPE.get(zone_name, 1.25)
    return (road_w_m + setback_m) * slope

def north_setback_limit(dist_from_north_m: float, zone_name: str) -> float:
    """
    北側斜線制限による高さ上限。
    dist_from_north_m: 敷地の北境界から建物北端までの距離(m)。
    適用外の用途地域では None を返す。
    """
    rise = NORTH_RISE.get(zone_name, 0)
    if rise == 0: return None
    return rise + max(0, dist_from_north_m) * 1.25

def calc_height_limits(
    zone_name: str,
    road_w_m: float,
    site_w: float,
    site_d: float,
    bldg_w: float,
    bldg_d: float,
    road_bearing_deg: float,
) -> dict:
    """
    道路斜線・北側斜線の高さ制限を計算して返す。

    戻り値:
    {
      "road_setback_m": float,         # 建物前面の道路境界からの後退距離(m)
      "road_slope": float,             # 道路斜線勾配
      "road_limit_h": float,           # 道路斜線による高さ制限(m)
      "north_rise": int or 0,          # 北側斜線立ち上がり(m)、0=適用外
      "dist_to_north_m": float,        # 建物北端→敷地北境界の距離(m)
      "north_limit_h": float or None,  # 北側斜線による高さ制限(m)、None=適用外
    }
    """
    slope = ROAD_SLOPE.get(zone_name, 1.25)

    # 建物のプロット座標での位置（site_wとsite_dをもとに中央寄せを仮定）
    ratio   = site_w / site_d if site_d > 0 else 1.0
    bldg_area = bldg_w * bldg_d
    # _create_volume_3d と同じ計算
    pad_x = max((site_w - bldg_w) / 2, 0)
    pad_y = max((site_d - bldg_d) / 2, 0)

    # 道路（Y=0）からの建物前面後退距離
    road_setback = pad_y   # Y=0 が道路側

    # 道路斜線: 建物前面位置での許容高さ
    road_lim = road_setback_limit(road_w_m, road_setback, zone_name)

    # 北側斜線
    nx, ny = _north_in_plot(road_bearing_deg)
    # 敷地の北側最大値（プロット座標で north_vec 方向）
    site_corners = [(0,0),(site_w,0),(site_w,site_d),(0,site_d)]
    north_max = max(x*nx + y*ny for x,y in site_corners)
    # 建物の北端（最も north_vec 方向に遠い角）
    bldg_corners = [(pad_x,pad_y),(pad_x+bldg_w,pad_y),(pad_x+bldg_w,pad_y+bldg_d),(pad_x,pad_y+bldg_d)]
    bldg_north = max(x*nx + y*ny for x,y in bldg_corners)
    dist_to_north = max(0.0, north_max - bldg_north)

    north_lim = north_setback_limit(dist_to_north, zone_name)
    rise = NORTH_RISE.get(zone_name, 0)

    return {
        "road_setback_m":  road_setback,
        "road_slope":      slope,
        "road_limit_h":    road_lim,
        "north_rise":      rise,
        "dist_to_north_m": dist_to_north,
        "north_limit_h":   north_lim,
    }

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 斜線エンベロープの Plotly トレース
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def road_setback_traces(road_w_m, site_w, site_d, zone_name, h_limit=50.0):
    """道路斜線エンベロープの斜面を Plotly Mesh3d トレースで返す（アンバー）。"""
    import plotly.graph_objects as go
    slope = ROAD_SLOPE.get(zone_name, 1.25)
    # 道路は Y=0 側 → 斜線は Y 方向に後退するほど高くなる
    n_y = 30  # Y方向分割数
    dy  = site_d / n_y
    vx, vy, vz = [], [], []
    for iy in range(n_y + 1):
        y  = iy * dy
        h  = min((road_w_m + y) * slope, h_limit)
        vx.extend([0, site_w])
        vy.extend([y, y])
        vz.extend([h, h])

    ii, jj, kk = [], [], []
    for iy in range(n_y):
        b = iy * 2
        ii.extend([b,   b+1]); jj.extend([b+1, b+2]); kk.extend([b+2, b+3])

    return [go.Mesh3d(
        x=vx, y=vy, z=vz, i=ii, j=jj, k=kk,
        color="#F5A524", opacity=0.20,
        name="道路斜線エンベロープ",
        showlegend=True, hoverinfo="none",
    )]

def north_setback_traces(road_bearing_deg, site_w, site_d, zone_name, h_limit=50.0):
    """北側斜線エンベロープの斜面を Plotly Mesh3d トレースで返す（グリーン）。"""
    import plotly.graph_objects as go
    rise = NORTH_RISE.get(zone_name, 0)
    if rise == 0: return []
    nx, ny = _north_in_plot(road_bearing_deg)
    # 敷地内で「北側境界（site_corners の最北端）」を求め、そこから距離を計算
    site_corners = [(0,0),(site_w,0),(site_w,site_d),(0,site_d)]
    north_max = max(x*nx + y*ny for x,y in site_corners)
    # グリッド点ごとに北側境界からの距離 → 許容高さ面を作成
    n_x, n_y_div = 20, 20
    dx = site_w / n_x; dy_div = site_d / n_y_div
    vx, vy, vz = [], [], []
    for iy in range(n_y_div + 1):
        y = iy * dy_div
        for ix in range(n_x + 1):
            x = ix * dx
            north_proj = x*nx + y*ny
            dist = max(0, north_max - north_proj)
            h    = min(rise + dist * 1.25, h_limit)
            vx.append(x); vy.append(y); vz.append(h)

    stride = n_x + 1
    ii, jj, kk = [], [], []
    for iy in range(n_y_div):
        for ix in range(n_x):
            b = iy * stride + ix
            ii.extend([b,   b+1])
            jj.extend([b+1, b+stride])
            kk.extend([b+stride, b+stride+1])

    return [go.Mesh3d(
        x=vx, y=vy, z=vz, i=ii, j=jj, k=kk,
        color="#3CB371", opacity=0.20,
        name="北側斜線エンベロープ",
        showlegend=True, hoverinfo="none",
    )]
